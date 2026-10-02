"""One OpenAI-compatible client for every provider.

Groq, Gemini's compatibility endpoint, OpenRouter and a local Ollama all speak the
same chat-completions API, so the only thing that varies is `base_url`, the key and
the model id. That is the whole reason the runtime works without a paid key
(PLAN.md 4.6).

Two things this layer owns beyond transport:

* **Fallback.** A 429 or a timeout moves to the next provider, honouring `retry-after`
  rather than hammering. When every provider is exhausted the caller gets a typed
  failure, and the pipeline degrades — verified queries still work with no LLM at all.
* **Budget enforcement.** Every call is costed before it is made and charged after,
  against both the provider's rate limit and the per-question ceiling.

Prompt hygiene lives here too: static system prompts go first so provider-side prompt
caching can take them, and values sampled from the database are always quoted as data.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .budget import Budgeter, BudgetExceeded, ProviderBudget, QuestionBudget, estimate_messages

PROVIDERS_FILE = Path(__file__).parent / "providers.yaml"


class LLMUnavailable(Exception):
    """No provider could serve this call."""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    models: dict[str, str]
    requires_key: bool = True
    notes: str = ""
    # Extra parameters this provider accepts and others reject, such as the
    # `reasoning_effort` the gpt-oss models take.
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.api_key) or not self.requires_key

    def model_for(self, role: str) -> str:
        return self.models.get(role) or self.models.get("large") or ""


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_ms: int
    cached: bool = False
    reasoning_tokens: int = 0
    # The provider's own total, when it reports one. Gemini bills its thinking in
    # `total_tokens` but leaves it out of `completion_tokens`: one SQL answer reported
    # 19 completion tokens against a total of 670. Adding prompt and completion
    # ourselves would undercount the real cost by the whole of the thinking.
    reported_total: int = 0

    @property
    def total_tokens(self) -> int:
        return max(self.prompt_tokens + self.completion_tokens, self.reported_total)


@dataclass
class CallRecord:
    stage: str
    provider: str
    model: str
    tokens: int
    ms: int
    ok: bool
    error: str = ""


def load_providers(path: Path | None = None) -> list[Provider]:
    raw = yaml.safe_load((path or PROVIDERS_FILE).read_text("utf-8"))
    out: list[Provider] = []
    for spec in raw.get("providers", []):
        key_env = spec.get("api_key_env") or ""
        base_url = spec.get("base_url", "")
        if spec.get("base_url_env"):
            base_url = os.environ.get(spec["base_url_env"]) or base_url
        out.append(
            Provider(
                name=spec["name"],
                base_url=base_url,
                api_key=os.environ.get(key_env, "") if key_env else "",
                models=spec.get("models", {}),
                requires_key=spec.get("requires_key", True),
                notes=spec.get("notes", ""),
                params=spec.get("params") or {},
            )
        )
    return out


def load_budgets(path: Path | None = None) -> dict[str, ProviderBudget]:
    raw = yaml.safe_load((path or PROVIDERS_FILE).read_text("utf-8"))
    out: dict[str, ProviderBudget] = {}
    for spec in raw.get("providers", []):
        limits = spec.get("limits") or {}
        out[spec["name"]] = ProviderBudget(
            name=spec["name"],
            tokens_per_minute=limits.get("tokens_per_minute", 0),
            requests_per_minute=limits.get("requests_per_minute", 0),
            tokens_per_day=limits.get("tokens_per_day", 0),
            requests_per_day=limits.get("requests_per_day", 0),
        )
    return out


class LLMClient:
    """Role-routed, budget-aware, provider-falling-back chat completions."""

    def __init__(
        self,
        providers: list[Provider] | None = None,
        budgeter: Budgeter | None = None,
        *,
        cache_dir: Path | None = None,
        max_wait_s: float = 25.0,
        offline: bool = False,
        only: list[str] | None = None,
    ):
        # Providers that could not be reached at all. Kept per client so one refused
        # connection does not cost a retry on every subsequent call, and — more
        # importantly — does not end up named as the reason a different provider failed.
        self.unreachable: set[str] = set()
        self.providers = [p for p in (providers or load_providers()) if p.usable]
        # A measurement has to name its model. Without a pin, a rate limit silently
        # hands the rest of a run to whichever provider is next, and one report ends up
        # averaging two models. `STRATUM_PROVIDERS=gemini,gemini-b` or `only=` pins it.
        pinned = only or [
            n.strip() for n in os.environ.get("STRATUM_PROVIDERS", "").split(",") if n.strip()
        ]
        if pinned:
            self.providers = [p for p in self.providers if p.name in pinned]
        # When false, cached completions are neither read nor counted. An eval sets it:
        # a replayed answer costs 0 tokens, so a second run of the same suite would
        # report a token cost that never happened. `STRATUM_LLM_REPLAY=0` turns it off
        # for a whole engine, for recording real runs.
        self.read_cache = os.environ.get("STRATUM_LLM_REPLAY", "1") != "0"
        self.budgeter = budgeter or Budgeter(load_budgets())
        self.cache_dir = cache_dir
        self.max_wait_s = max_wait_s
        # `offline` makes every call raise instead of reaching the network. The eval
        # harness uses it to measure the pipeline's deterministic half on its own.
        self.offline = offline
        self.calls: list[CallRecord] = []
        self._clients: dict[str, Any] = {}
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return bool(self.providers) and not self.offline

    def provider_order(self, role: str) -> list[str]:
        return [
            p.name for p in self.providers
            if p.model_for(role) and p.name not in self.unreachable
        ]

    # ---------------------------------------------------------------------- cache
    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{key}.json"

    @staticmethod
    def cache_key(
        messages: list[dict], model_role: str, temperature: float,
        models: list[str] | None = None,
    ) -> str:
        import hashlib

        payload = json.dumps(
            {"m": messages, "r": model_role, "t": temperature, "models": models or []},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    # ----------------------------------------------------------------------- call
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "large",
        stage: str = "",
        question_budget: QuestionBudget | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> Completion:
        estimated = estimate_messages(messages) + max_tokens

        # The key names the models that could answer. Without them a completion cached
        # from one provider is replayed for another, and a run labelled with one model
        # is partly the output of a different one.
        models = [f"{p.name}:{p.model_for(role)}" for p in self.providers if p.model_for(role)]
        key = self.cache_key(messages, role, temperature, models)
        cache_path = self._cache_path(key) if use_cache else None
        if cache_path is not None and self.read_cache and cache_path.exists():
            cached = json.loads(cache_path.read_text("utf-8"))
            return Completion(
                text=cached["text"], provider=cached.get("provider", "cache"),
                model=cached.get("model", ""), prompt_tokens=0, completion_tokens=0,
                elapsed_ms=0, cached=True,
            )

        if question_budget is not None and not question_budget.can_afford(estimated):
            raise BudgetExceeded(
                f"{stage or role}: needs about {estimated} tokens but only "
                f"{question_budget.remaining} remain of the {question_budget.limit} "
                "allowed for this question"
            )
        if self.offline:
            raise LLMUnavailable("client is in offline mode")
        if not self.providers:
            raise LLMUnavailable(
                "no LLM provider is configured — set GROQ_API_KEY or one of the "
                "alternatives in .env; verified queries still work without one"
            )

        order = self.provider_order(role)
        if not order:
            raise LLMUnavailable(
                "no provider can serve this call: "
                + (
                    f"unreachable: {', '.join(sorted(self.unreachable))}"
                    if self.unreachable else "none configured for this role"
                )
            )
        # Why each provider could not be used, so the message names the real blocker
        # rather than whichever provider happened to be tried last.
        errors: list[str] = []
        rate_limited: list[str] = []
        for _attempt in range(len(order) + 1):
            name, wait = self.budgeter.pick_provider(order, estimated)
            if name is None:
                break
            if wait > 0:
                if wait > self.max_wait_s:
                    rate_limited.append(f"{name}: rate limited for another {wait:.0f}s")
                    order = [n for n in order if n != name]
                    if not order:
                        break
                    continue
                time.sleep(wait)

            provider = next((p for p in self.providers if p.name == name), None)
            if provider is None:
                order = [n for n in order if n != name]
                continue

            try:
                completion = self._call(
                    provider, messages, role, temperature, max_tokens, json_mode
                )
            except Exception as exc:
                self.calls.append(
                    CallRecord(stage, provider.name, provider.model_for(role), 0, 0,
                               False, str(exc)[:200])
                )
                # A reasoning model that thought through its whole budget answered
                # nothing, but it is not unavailable: the same request with room to
                # finish usually succeeds. Once, doubled, and only if the question's
                # token ceiling can still afford it — never by dropping the provider,
                # which turned one tight budget into an abstention.
                if (
                    "reasoning and returned nothing" in str(exc)
                    and max_tokens < REASONING_RETRY_CEILING
                ):
                    bigger = min(max_tokens * 2, REASONING_RETRY_CEILING)
                    needed = estimate_messages(messages) + bigger
                    if question_budget is None or question_budget.can_afford(needed):
                        max_tokens, estimated = bigger, needed
                        continue
                retry_after = _retry_after(exc)
                if retry_after:
                    self.budgeter.back_off(provider.name, retry_after)
                    # A rate limit says "later", not "never". Dropping the provider here
                    # turned a three-minute cooldown into an abstention for every
                    # remaining question in the run. Wait it out when the caller's
                    # patience allows, and only step aside when it does not.
                    rate_limited.append(
                        f"{provider.name}: rate limited for {retry_after:.0f}s"
                    )
                    if retry_after <= self.max_wait_s:
                        time.sleep(retry_after)
                        continue
                    order = [n for n in order if n != provider.name]
                    if not order:
                        break
                    continue

                if _is_unreachable(exc):
                    # Not configured, not running, or no network. Retrying it on every
                    # call wastes time and buries the real error.
                    self.unreachable.add(provider.name)
                else:
                    errors.append(f"{provider.name}: {exc}")
                order = [n for n in order if n != provider.name]
                if not order:
                    break
                continue

            self.budgeter.record(provider.name, completion.total_tokens)
            if question_budget is not None:
                question_budget.charge(stage or role, completion.total_tokens)
            self.calls.append(
                CallRecord(stage, provider.name, completion.model,
                           completion.total_tokens, completion.elapsed_ms, True)
            )
            if cache_path is not None:
                cache_path.write_text(
                    json.dumps({
                        "text": completion.text,
                        "provider": completion.provider,
                        "model": completion.model,
                    }),
                    "utf-8",
                )
            return completion

        # Rate limiting is the interesting case and belongs first: it means "wait",
        # not "broken", and the caller may want to retry rather than abstain.
        reasons = rate_limited + errors
        if not reasons and self.unreachable:
            reasons = [f"unreachable: {', '.join(sorted(self.unreachable))}"]
        raise LLMUnavailable(
            "every provider failed or is rate limited: " + "; ".join(reasons[:4])
        )

    def _call(
        self,
        provider: Provider,
        messages: list[dict[str, str]],
        role: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> Completion:
        from openai import OpenAI

        client = self._clients.get(provider.name)
        if client is None:
            client = OpenAI(
                base_url=provider.base_url,
                api_key=provider.api_key or "not-needed",
                timeout=60.0,
                max_retries=0,          # retries are this class's job, not the SDK's
            )
            self._clients[provider.name] = client

        model = provider.model_for(role)
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **provider.params,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**kwargs)
        elapsed = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage", None)
        text = (response.choices[0].message.content or "") if response.choices else ""

        reasoning_tokens = 0
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None:
            reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0

        # A reasoning model whose budget was eaten by its own thinking returns an empty
        # string with a 200. Left alone that reads as "the model declined to answer",
        # which sends the caller down the repair path for a problem repair cannot fix.
        if not text.strip() and reasoning_tokens:
            raise RuntimeError(
                f"{model} used all {max_tokens} tokens reasoning and returned nothing; "
                "raise max_tokens or lower reasoning_effort"
            )

        return Completion(
            text=text,
            provider=provider.name,
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            elapsed_ms=elapsed,
            reasoning_tokens=reasoning_tokens,
            reported_total=getattr(usage, "total_tokens", 0) or 0,
        )

    # ------------------------------------------------------------------- helpers
    def complete_json(
        self, messages: list[dict[str, str]], **kwargs
    ) -> tuple[Any, Completion]:
        """Ask for JSON and parse it, tolerating the fences models add anyway."""
        kwargs.setdefault("json_mode", True)
        completion = self.complete(messages, **kwargs)
        return parse_json(completion.text), completion

    def usage(self) -> dict[str, Any]:
        ok = [c for c in self.calls if c.ok]
        return {
            "calls": len(self.calls),
            "failed": len(self.calls) - len(ok),
            "tokens": sum(c.tokens for c in ok),
            "ms": sum(c.ms for c in ok),
            "by_stage": _group(ok),
            "budget": self.budgeter.snapshot(),
        }


def _group(calls: list[CallRecord]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for call in calls:
        bucket = out.setdefault(call.stage or "?", {"calls": 0, "tokens": 0})
        bucket["calls"] += 1
        bucket["tokens"] += call.tokens
    return out


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json(text: str) -> Any:
    """Parse a model's JSON, coping with the ways models fail to emit clean JSON."""
    if not text or not text.strip():
        return None
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost object or array in the response.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _is_unreachable(exc: Exception) -> bool:
    """Is this a provider that is simply not there, rather than one that refused us?"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "connection error", "connection refused", "connect call failed",
            "name or service not known", "failed to establish", "getaddrinfo",
            "max retries exceeded", "apiconnectionerror",
        )
    )


# A server that is up but busy. Gemini answers demand spikes with a 503 "high demand",
# which is the same message as a 429 — later, not never — but carries no retry-after.
# Treated as permanent, it dropped the provider for the call and cost 6 of 10
# enrichment calls on a single excavation.
_TRANSIENT = re.compile(
    r"\b(502|503|504)\b|high demand|overloaded|temporarily unavailable|UNAVAILABLE"
)
TRANSIENT_BACKOFF_S = 8.0

# The largest output budget a reasoning retry may grow to.
REASONING_RETRY_CEILING = 2000


def _retry_after(exc: Exception) -> float:
    """Pull a retry-after out of a rate-limit or overload error, in seconds."""
    text = str(exc)
    if _TRANSIENT.search(text) and "429" not in text:
        return TRANSIENT_BACKOFF_S
    if "429" not in text and "rate" not in text.lower():
        return 0.0
    match = re.search(r"retry[- ]after[\"':\s]+([\d.]+)", text, re.IGNORECASE)
    if match:
        return min(120.0, float(match.group(1)))
    # Groq phrases it as "Please try again in 3m31.248s"; others use plain seconds.
    match = re.search(
        r"try again in (?:([\d.]+)m)?(?:([\d.]+)s)?", text, re.IGNORECASE
    )
    if match and (match.group(1) or match.group(2)):
        minutes = float(match.group(1) or 0)
        seconds = float(match.group(2) or 0)
        return min(600.0, minutes * 60 + seconds)
    return 20.0


def quote_as_data(value: Any, limit: int = 60) -> str:
    """Render a database value for a prompt as unmistakably *data*.

    Prompt-injection hygiene (PLAN.md 4.8): a row that contains "ignore previous
    instructions" must arrive as a quoted string, not as a line of the prompt.
    """
    text = "NULL" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "")
    if len(text) > limit:
        text = text[:limit] + "…"
    return f'"{text}"'
