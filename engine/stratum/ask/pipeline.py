"""The online pipeline: a question in, an answer out (PLAN.md 4.4).

    route -> understand -> ambiguity? -> link -> plan joins -> generate -> compile
          -> validate -> execute -> repair -> select -> confidence -> explain

Every stage emits a trace event over SSE, and the eval harness consumes the same
events. The deterministic stages run whether or not an LLM is reachable; without one
the pipeline still answers verified questions and still refuses unsafe SQL, which is
what "graceful degradation" has to mean when the runtime is a free tier.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..connectors.costguard import CostGuard
from ..connectors.executor import ExecutionError, QueryResult, ReadOnlyExecutor
from ..llm.budget import QuestionBudget
from ..llm.client import LLMClient, LLMUnavailable
from ..semantic.model import SemanticModel
from . import ambiguity as ambiguity_module
from . import generate as generate_module
from .cache import SemanticCache
from .compiler import CompiledQuery, CompileError, compile_logical
from .confidence import ConfidenceScore, gate, score_answer
from .joinplan import JoinPlan, build_graph
from .joinplan import plan as plan_joins
from .linking import LinkResult, SchemaIndex
from .validate import ValidationResult, validate
from .values import ValueBinding, ValueIndex, render_bindings

MAX_REPAIRS = 3


@dataclass
class TraceEvent:
    stage: str
    ms: int
    tokens: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "ms": self.ms, "tokens": self.tokens,
                "detail": self.detail}


@dataclass
class Answer:
    question: str
    status: str = "ok"              # ok | clarify | abstain | cost_guard | error
    logical_sql: str = ""
    physical_sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    row_count: int = 0

    entities: list[str] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    confidence: ConfidenceScore | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    validation: ValidationResult | None = None
    join_plan: JoinPlan | None = None
    linking: LinkResult | None = None

    clarification: dict[str, Any] | None = None
    value_bindings: list[dict[str, Any]] = field(default_factory=list)
    cache_hit: bool = False
    cost_warning: dict[str, Any] | None = None
    explanation: str = ""
    message: str = ""
    verified_hit: bool = False
    repairs: int = 0
    candidates_run: int = 0
    candidate_agreement: float = 1.0

    trace: list[TraceEvent] = field(default_factory=list)
    tokens: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "logical_sql": self.logical_sql,
            "physical_sql": self.physical_sql,
            "columns": self.columns,
            "rows": [list(r) for r in self.rows[:200]],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "entities": self.entities,
            "assumptions": self.assumptions,
            "confidence": self.confidence.to_dict() if self.confidence else None,
            "gate": self.gate,
            "validation": self.validation.to_dict() if self.validation else None,
            "join_plan": self.join_plan.to_dict() if self.join_plan else None,
            "clarification": self.clarification,
            "value_bindings": self.value_bindings,
            "cache_hit": self.cache_hit,
            "cost_warning": self.cost_warning,
            "explanation": self.explanation,
            "message": self.message,
            "verified_hit": self.verified_hit,
            "repairs": self.repairs,
            "tokens": self.tokens,
            "elapsed_ms": self.elapsed_ms,
            "trace": [e.to_dict() for e in self.trace],
        }


Emit = Callable[[TraceEvent], None]


@dataclass
class AskContext:
    """Everything a question needs, assembled once per database rather than per ask."""

    model: SemanticModel
    executor: ReadOnlyExecutor
    llm: LLMClient | None = None
    cost_guard: CostGuard | None = None
    verified: Any = None                 # VerifiedLibrary, when one is loaded
    cache: SemanticCache | None = None
    profiles: Any = None                 # ProfileSet, for the value index
    index: SchemaIndex | None = None
    values: ValueIndex | None = None
    graph: Any = None

    def __post_init__(self) -> None:
        if self.index is None:
            self.index = SchemaIndex(self.model)
        if self.graph is None:
            self.graph = build_graph(self.model)
        if self.values is None:
            self.values = ValueIndex(self.model, self.profiles)


def ask(
    context: AskContext,
    question: str,
    *,
    mode: str = "default",
    clarification: str = "",
    approved_entities: list[str] | None = None,
    allow_expensive: bool = False,
    token_limit: int = 6000,
    emit: Emit | None = None,
) -> Answer:
    started = time.perf_counter()
    answer = Answer(question=question)
    budget = QuestionBudget(limit=token_limit)

    def stage(name: str):
        stage_started = time.perf_counter()

        def done(**detail: Any) -> None:
            spent = budget.spent - getattr(done, "_before", budget.spent)
            event = TraceEvent(
                stage=name,
                ms=int((time.perf_counter() - stage_started) * 1000),
                tokens=max(0, spent),
                detail=detail,
            )
            answer.trace.append(event)
            if emit is not None:
                emit(event)

        done._before = budget.spent      # noqa: SLF001 - closure state, deliberate
        return done

    # ------------------------------------------------------------------ 1. route
    done = stage("route")
    if context.verified is not None:
        hit = context.verified.match(question)
        if hit is not None:
            done(hit="verified", question=hit.question)
            return _run_verified(context, answer, hit, started, emit)

    # The semantic cache sits on the same rung but means something weaker: this is
    # something the system said before, not something a person confirmed.
    cached = None
    if context.cache is not None:
        cached = context.cache.lookup(question, context.model.version)
    if cached is not None:
        done(hit="cache", question=cached.question)
        replayed = _run_cached(context, answer, cached, started)
        if replayed is not None:
            return replayed
    done(hit=False)

    # ------------------------------------------------------------------- 2. link
    done = stage("link")
    linked = context.index.search(question)
    if approved_entities:
        entities = [e for e in approved_entities if context.model.entity(e)]
        linked.needs_confirmation = False
    else:
        entities = linked.names
    answer.linking = linked
    answer.entities = entities

    bindings: list[ValueBinding] = []
    if context.values is not None:
        bindings = context.values.bind(question, entities)
        answer.value_bindings = [b.to_dict() for b in bindings]
    done(entities=entities, margin=round(linked.margin, 3),
         needs_confirmation=linked.needs_confirmation,
         values_bound=[b.literal for b in bindings])

    if not entities:
        answer.status = "abstain"
        answer.message = (
            "Nothing in this database appears to relate to that question. "
            "The model covers: " + ", ".join(sorted(context.model.entities)[:12])
        )
        answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return answer


    # -------------------------------------------------------------- 3. ambiguity
    # Skipped when the caller already supplied a clarification: asking twice about the
    # same thing is worse than guessing once.
    if not clarification:
        done = stage("ambiguity")
        found = ambiguity_module.detect(question, context.model, entities=entities)
        done(needs_clarification=found.needs_clarification, reason=found.reason)

        if found.needs_clarification and found.primary is not None:
            options = ambiguity_module.build_options(
                found.primary, context.model, entities, question
            )
            answer.status = "clarify"
            answer.clarification = {
                **found.primary.to_dict(),
                "options": [o.to_dict() for o in options],
            }
            answer.message = found.primary.question
            answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return answer

    # --------------------------------------------------------------- 4. join plan
    done = stage("joinplan")
    join_plan = plan_joins(context.model, entities, graph=context.graph)
    answer.join_plan = join_plan
    done(**join_plan.to_dict())

    # ---------------------------------------------------------------- 4. generate
    if context.llm is None or not context.llm.available:
        answer.status = "abstain"
        answer.message = (
            "No language model is reachable, so a new query cannot be written. "
            "Verified queries still work."
        )
        answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return answer

    done = stage("generate")
    columns = {e.entity: e.columns for e in linked.entities}
    examples = (
        context.verified.examples_for(entities) if context.verified is not None else []
    )
    generated = generate_module.generate(
        context.llm, question, context.model, entities, join_plan,
        columns=columns,
        examples=examples,
        glossary=context.model.glossary,
        value_bindings=render_bindings(bindings),
        dialect=context.executor.profile.sqlglot_dialect,
        clarification=clarification,
        candidates=2 if mode == "default" else 3,
        question_budget=budget,
    )
    answer.candidates_run = len(generated.usable)
    done(candidates=len(generated.usable), aborted=generated.aborted_reason)

    if not generated.usable:
        answer.status = "abstain"
        answer.message = (
            generated.aborted_reason
            or "The language model did not return usable SQL."
        )
        answer.tokens = budget.spent
        answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return answer

    # ------------------------------------- 5. compile, validate, execute, repair
    attempts: list[dict[str, Any]] = []
    for candidate in generated.usable:
        outcome = _try_candidate(
            context, answer, candidate.logical_sql, question, entities, columns,
            budget, allow_expensive, emit, stage,
        )
        attempts.append(outcome)
        if outcome.get("cost_blocked"):
            answer.status = "cost_guard"
            answer.cost_warning = outcome["cost"]
            answer.message = (
                "This query would read far more data than it should. "
                + outcome["cost"].get("reason", "")
            )
            answer.physical_sql = outcome.get("physical_sql", "")
            answer.logical_sql = outcome.get("logical_sql", "")
            answer.tokens = budget.spent
            answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return answer

    successful = [a for a in attempts if a.get("result") is not None]
    if not successful:
        first = attempts[0] if attempts else {}
        answer.status = "abstain"
        answer.message = first.get("error", "the query could not be made to run")
        answer.logical_sql = first.get("logical_sql", "")
        answer.physical_sql = first.get("physical_sql", "")
        answer.validation = first.get("validation")
        answer.tokens = budget.spent
        answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return answer

    # ------------------------------------------------------------------ 6. select
    done = stage("select")
    chosen, agreement = _select(successful)
    answer.candidate_agreement = agreement
    done(candidates=len(successful), agreement=round(agreement, 2))

    compiled: CompiledQuery = chosen["compiled"]
    result: QueryResult = chosen["result"]
    answer.logical_sql = compiled.logical_sql
    answer.physical_sql = compiled.physical_sql
    answer.columns = result.columns
    answer.rows = result.rows
    answer.row_count = result.row_count
    answer.truncated = result.truncated
    answer.assumptions = [a.to_dict() for a in compiled.assumptions]
    answer.validation = chosen.get("validation")
    answer.repairs = chosen.get("repairs", 0)
    answer.entities = compiled.entities_used or entities

    # -------------------------------------------------------------- 7. confidence
    done = stage("confidence")
    answer.confidence = score_answer(
        model=context.model,
        entities_used=answer.entities,
        join_plan=join_plan,
        validation=answer.validation,
        candidate_agreement=agreement,
        candidates_run=answer.candidates_run,
        repairs=answer.repairs,
        linking_margin=linked.margin,
        executed=True,
        row_count=answer.row_count,
    )
    answer.gate = gate(answer.confidence)
    done(score=answer.confidence.score, band=answer.confidence.band)

    if answer.gate["action"] == "abstain":
        answer.status = "abstain"
        answer.message = answer.gate["message"]

    # --------------------------------------------------------------- 8. explain
    done = stage("explain")
    from .explain import explain_answer

    answer.explanation = explain_answer(answer, context.model)
    done(assumptions=len(answer.assumptions))

    if (
        context.cache is not None
        and answer.status == "ok"
        and answer.confidence is not None
    ):
        context.cache.put(
            question, answer.logical_sql, context.model.version,
            entities=answer.entities, confidence=answer.confidence.score,
        )

    answer.tokens = budget.spent
    answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return answer


def _run_cached(context: AskContext, answer: Answer, cached, started: float):
    """Replay a cached query. Returns None if it no longer runs, so the caller falls
    through to generating a fresh one rather than reporting a failure."""
    try:
        compiled = compile_logical(
            cached.logical_sql, context.model, context.executor.profile
        )
        result = context.executor.run(compiled.physical_sql)
    except Exception:
        return None

    answer.cache_hit = True
    answer.logical_sql = cached.logical_sql
    answer.physical_sql = compiled.physical_sql
    answer.columns = result.columns
    answer.rows = result.rows
    answer.row_count = result.row_count
    answer.truncated = result.truncated
    answer.entities = compiled.entities_used
    answer.assumptions = [a.to_dict() for a in compiled.assumptions]
    answer.confidence = score_answer(
        model=context.model, entities_used=compiled.entities_used, join_plan=None,
        validation=None,
        # A cached answer inherits the confidence it earned, discounted: the data may
        # have moved since, and nobody re-checked it.
        candidate_agreement=cached.confidence * 0.9, candidates_run=1, repairs=0,
        linking_margin=1.0, row_count=result.row_count,
    )
    answer.gate = gate(answer.confidence)
    from .explain import explain_answer

    answer.explanation = explain_answer(answer, context.model)
    answer.explanation += (
        "\n\nThis repeats a query answered earlier in this session; the data was re-read."
    )
    answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return answer


def _try_candidate(
    context: AskContext,
    answer: Answer,
    logical_sql: str,
    question: str,
    entities: list[str],
    columns: dict[str, list[str]],
    budget: QuestionBudget,
    allow_expensive: bool,
    emit: Emit | None,
    stage,
) -> dict[str, Any]:
    """Compile, validate, execute, and repair one candidate up to MAX_REPAIRS times."""
    outcome: dict[str, Any] = {"logical_sql": logical_sql, "repairs": 0}
    current = logical_sql

    for attempt in range(MAX_REPAIRS + 1):
        done = stage("compile" if attempt == 0 else f"repair:{attempt}")
        try:
            compiled = compile_logical(current, context.model, context.executor.profile)
        except CompileError as exc:
            done(error=str(exc)[:200])
            outcome["error"] = str(exc)
            current = _repair(
                context, question, current, str(exc), entities, columns, budget
            )
            outcome["repairs"] = attempt + 1
            if current is None:
                return outcome
            continue
        done(entities=compiled.entities_used, downgrades=compiled.downgrades)

        outcome["compiled"] = compiled
        outcome["physical_sql"] = compiled.physical_sql

        if compiled.unresolved:
            outcome["error"] = (
                "the query needs SQL this connection's profile forbids: "
                + "; ".join(compiled.unresolved[:3])
            )
            return outcome

        done = stage("validate")
        checks = validate(
            compiled, context.model, context.executor,
            cost_guard=context.cost_guard if not allow_expensive else None,
        )
        outcome["validation"] = checks
        done(ok=checks.ok, failed=[c.name for c in checks.blocking])

        cost_check = next((c for c in checks.checks if c.name == "cost_guard"), None)
        if cost_check is not None and cost_check.failed:
            outcome["cost_blocked"] = True
            outcome["cost"] = cost_check.detail
            return outcome

        blocking = [c for c in checks.blocking if c.name != "cost_guard"]
        if blocking:
            message = "; ".join(c.message for c in blocking)
            outcome["error"] = message
            if attempt >= MAX_REPAIRS:
                return outcome
            current = _repair(
                context, question, current, message, entities,
                {c.name: c.detail.get("available", {}) for c in blocking}
                or columns,
                budget,
            )
            outcome["repairs"] = attempt + 1
            if current is None:
                return outcome
            continue

        done = stage("execute")
        try:
            result = context.executor.run(compiled.physical_sql)
        except ExecutionError as exc:
            done(error=str(exc)[:200])
            outcome["error"] = str(exc)
            if attempt >= MAX_REPAIRS:
                return outcome
            current = _repair(
                context, question, current, str(exc), entities, columns, budget
            )
            outcome["repairs"] = attempt + 1
            if current is None:
                return outcome
            continue

        done(rows=result.row_count, ms=result.elapsed_ms)
        outcome["result"] = result
        return outcome

    return outcome


def _repair(
    context: AskContext,
    question: str,
    failed_sql: str,
    error: str,
    entities: list[str],
    available: dict[str, list[str]],
    budget: QuestionBudget,
) -> str | None:
    """Ask the model to fix its own SQL, given the error and the real names."""
    if context.llm is None or not context.llm.available:
        return None
    messages = generate_module.repair_prompt(
        question, failed_sql, error, context.model, entities, available=available
    )
    try:
        completion = context.llm.complete(
            messages, role="large", stage="repair",
            question_budget=budget, temperature=0.0, max_tokens=450,
        )
    except (LLMUnavailable, Exception):
        return None
    repaired = generate_module.clean_sql(completion.text)
    return repaired or None


def _select(attempts: list[dict[str, Any]]) -> tuple[dict[str, Any], float]:
    """Group candidates by result-set equality; the majority wins (PLAN.md 4.4 step 11).

    Comparing *results* rather than SQL text is what makes this work: two very
    differently written queries that return the same rows are strong evidence, and two
    similar-looking queries that return different rows are exactly the case worth
    flagging.
    """
    if len(attempts) == 1:
        return attempts[0], 0.6

    def fingerprint(attempt: dict[str, Any]) -> str:
        result = attempt["result"]
        rows = sorted(str(r) for r in result.rows[:200])
        return f"{sorted(result.columns)}|{rows}"

    groups: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        groups.setdefault(fingerprint(attempt), []).append(attempt)

    largest = max(groups.values(), key=len)
    agreement = len(largest) / len(attempts)
    # Within the winning group prefer the candidate that needed the fewest repairs.
    chosen = min(largest, key=lambda a: a.get("repairs", 0))
    return chosen, agreement


def _run_verified(
    context: AskContext, answer: Answer, hit, started: float, emit: Emit | None
) -> Answer:
    """Run a query a person has already verified. Zero generation tokens."""
    answer.verified_hit = True
    answer.logical_sql = hit.logical_sql
    try:
        compiled = compile_logical(
            hit.logical_sql, context.model, context.executor.profile
        )
        result = context.executor.run(compiled.physical_sql)
    except Exception as exc:
        answer.status = "error"
        answer.message = f"the verified query failed: {exc}"
        answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return answer

    answer.physical_sql = compiled.physical_sql
    answer.columns = result.columns
    answer.rows = result.rows
    answer.row_count = result.row_count
    answer.entities = compiled.entities_used
    answer.assumptions = [a.to_dict() for a in compiled.assumptions]
    answer.confidence = score_answer(
        model=context.model, entities_used=compiled.entities_used, join_plan=None,
        validation=None, candidate_agreement=1.0, candidates_run=1, repairs=0,
        linking_margin=1.0, verified_hit=True, row_count=result.row_count,
    )
    answer.gate = gate(answer.confidence)
    from .explain import explain_answer

    answer.explanation = explain_answer(answer, context.model)
    answer.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return answer
