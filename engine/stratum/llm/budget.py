"""Token budgeting.

CLAUDE.md non-negotiable #4: the default runtime is a free tier of roughly 8K tokens
per minute, and a question in default mode must stay under ~6K total LLM tokens.

That is a hard constraint, not a target, so the budgeter is not advisory: it refuses
calls that would breach the per-question budget and makes the pipeline degrade — fewer
candidates, a smaller model, a shorter schema — rather than fail. A question that
cannot be answered inside the budget should come back as an honest abstention, not as
a 429.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(Exception):
    """This call would breach the per-question or per-provider budget."""


def estimate_tokens(text: str) -> int:
    """A cheap, deliberately pessimistic token estimate.

    Tokenising properly would mean shipping a tokeniser per provider. Roughly four
    characters per token is close enough for rationing, and erring high is the safe
    direction when the consequence of being wrong is a rate-limit stall mid-eval.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.6) + 1)


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    total = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
    return total + 4 * len(messages)      # per-message framing overhead


@dataclass
class Window:
    """A sliding window rate limiter over one resource."""

    limit: int
    seconds: float
    events: deque = field(default_factory=deque)

    def _prune(self, now: float) -> None:
        while self.events and now - self.events[0][0] > self.seconds:
            self.events.popleft()

    def used(self, now: float | None = None) -> int:
        now = now or time.monotonic()
        self._prune(now)
        return sum(amount for _, amount in self.events)

    def would_exceed(self, amount: int, now: float | None = None) -> bool:
        if not self.limit:
            return False
        return self.used(now) + amount > self.limit

    def wait_time(self, amount: int) -> float:
        """Seconds until `amount` would fit. 0 when it already does."""
        if not self.limit:
            return 0.0
        now = time.monotonic()
        self._prune(now)
        if self.used(now) + amount <= self.limit:
            return 0.0
        needed = self.used(now) + amount - self.limit
        freed = 0
        for timestamp, value in self.events:
            freed += value
            if freed >= needed:
                return max(0.0, self.seconds - (now - timestamp)) + 0.05
        return self.seconds

    def record(self, amount: int) -> None:
        self.events.append((time.monotonic(), amount))


@dataclass
class ProviderBudget:
    name: str
    tokens_per_minute: int = 0
    requests_per_minute: int = 0
    tokens_per_day: int = 0
    requests_per_day: int = 0

    def __post_init__(self) -> None:
        self.tpm = Window(self.tokens_per_minute, 60.0)
        self.rpm = Window(self.requests_per_minute, 60.0)
        self.tpd = Window(self.tokens_per_day, 86400.0)
        self.rpd = Window(self.requests_per_day, 86400.0)
        self.cooldown_until = 0.0        # set by a 429's retry-after

    def available(self, tokens: int) -> bool:
        if time.monotonic() < self.cooldown_until:
            return False
        return not (
            self.tpm.would_exceed(tokens)
            or self.rpm.would_exceed(1)
            or self.tpd.would_exceed(tokens)
            or self.rpd.would_exceed(1)
        )

    def wait_time(self, tokens: int) -> float:
        cooldown = max(0.0, self.cooldown_until - time.monotonic())
        return max(
            cooldown,
            self.tpm.wait_time(tokens),
            self.rpm.wait_time(1),
        )

    def record(self, tokens: int) -> None:
        self.tpm.record(tokens)
        self.rpm.record(1)
        self.tpd.record(tokens)
        self.rpd.record(1)

    def back_off(self, seconds: float) -> None:
        """Honour a provider's retry-after rather than hammering it."""
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "tokens_this_minute": self.tpm.used(),
            "tokens_per_minute_limit": self.tokens_per_minute,
            "requests_this_minute": self.rpm.used(),
            "tokens_today": self.tpd.used(),
            "cooling_down_for": round(max(0.0, self.cooldown_until - time.monotonic()), 1),
        }


@dataclass
class QuestionBudget:
    """The per-question ceiling. This is the one CLAUDE.md fixes at ~6K."""

    limit: int = 6000
    spent: int = 0
    calls: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def can_afford(self, tokens: int) -> bool:
        return self.spent + tokens <= self.limit

    def charge(self, stage: str, tokens: int) -> None:
        self.spent += tokens
        self.calls += 1
        self.by_stage[stage] = self.by_stage.get(stage, 0) + tokens

    def snapshot(self) -> dict[str, Any]:
        return {
            "spent": self.spent,
            "limit": self.limit,
            "remaining": self.remaining,
            "calls": self.calls,
            "by_stage": dict(sorted(self.by_stage.items(), key=lambda kv: -kv[1])),
        }


class Budgeter:
    """Rations tokens across providers and across the stages of one question.

    Thread-safe because the eval harness runs questions concurrently against the same
    provider limits.
    """

    def __init__(self, providers: dict[str, ProviderBudget] | None = None,
                 question_limit: int = 6000):
        self.providers = providers or {}
        self.question_limit = question_limit
        self._lock = threading.Lock()
        self.total_spent = 0

    def register(self, budget: ProviderBudget) -> None:
        with self._lock:
            self.providers[budget.name] = budget

    def new_question(self, limit: int | None = None) -> QuestionBudget:
        return QuestionBudget(limit=limit or self.question_limit)

    def pick_provider(
        self, order: list[str], tokens: int
    ) -> tuple[str | None, float]:
        """The first provider in `order` with room, or the one with the shortest wait."""
        with self._lock:
            best_wait = float("inf")
            best_name = None
            for name in order:
                budget = self.providers.get(name)
                if budget is None:
                    return name, 0.0        # unmetered provider, e.g. local Ollama
                if budget.available(tokens):
                    return name, 0.0
                wait = budget.wait_time(tokens)
                if wait < best_wait:
                    best_wait, best_name = wait, name
            return best_name, (best_wait if best_wait != float("inf") else 0.0)

    def record(self, provider: str, tokens: int) -> None:
        with self._lock:
            budget = self.providers.get(provider)
            if budget is not None:
                budget.record(tokens)
            self.total_spent += tokens

    def back_off(self, provider: str, seconds: float) -> None:
        with self._lock:
            budget = self.providers.get(provider)
            if budget is not None:
                budget.back_off(seconds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_spent": self.total_spent,
                "providers": [b.snapshot() for b in self.providers.values()],
            }
