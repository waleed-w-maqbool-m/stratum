"""The eval harness.

Runs a question suite against a legacy database and scores the answers against gold
computed on the clean copy. Supports the things a free-tier runtime forces:

* **Resume.** Every question's outcome is written as it completes, so a run stopped by
  a rate limit continues where it left off rather than starting again.
* **Baselines and ablations.** `--mode naive` is the "full schema dump, one LLM call"
  baseline the plan requires in every report; `--ablate` switches off one component so
  its contribution can be measured.

Usage:
    python -m gauntlet.runner --suite chinook_v1 --mode full
    python -m gauntlet.runner --suite chinook_v1 --mode naive
    python -m gauntlet.runner --suite chinook_v1 --ablate semantic_layer
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "engine"))

from stratum.config import load_env  # noqa: E402

load_env(HERE.parent)

from gauntlet.scorer import (  # noqa: E402
    QuestionOutcome,
    Suite,
    compare,
    render_report,
    score_run,
)

ABLATIONS = {
    "semantic_layer": "generate physical SQL directly against raw table names",
    "join_inference": "drop every inferred relationship from the model",
    "value_index": "do not map question literals onto stored values",
    "single_candidate": "generate one candidate instead of two",
    "no_repair": "do not attempt to fix a failed query",
    "no_cost_guard": "run expensive queries instead of stopping",
}


def load_context(
    url: str, database: str, *, ablate: set[str], offline: bool, patience: float = 150.0,
    providers: list[str] | None = None,
):
    """Excavate (or load a cached model) and assemble everything a question needs."""
    from stratum.ask.pipeline import AskContext
    from stratum.connectors.costguard import CostGuard
    from stratum.connectors.executor import ReadOnlyExecutor
    from stratum.connectors.registry import build
    from stratum.excavate.compile_model import compile_model
    from stratum.excavate.pipeline import excavate
    from stratum.llm.client import LLMClient
    from stratum.semantic.store import ModelStore
    from stratum.semantic.verified import VerifiedLibrary

    connector = build(url)
    connector.connect()

    store = ModelStore(HERE.parent / "engine" / ".stratum" / "models")

    # An eval run is the heaviest consumer of a free tier and has all the time in the
    # world. The naive baseline needs ~44 s of token budget per question against an
    # 8K/minute limit, so a 25 s ceiling would abandon most of the suite.
    llm = LLMClient(
        cache_dir=store.cache_dir(database), offline=offline, max_wait_s=patience,
        only=providers,
    )

    model = store.load(database)
    if model is None:
        result = excavate(connector, llm=None if offline else llm)
        model = compile_model(result, database=database, profile_name=connector.profile.name)
        store.save(model)
        store.save_findings(database, [f.to_dict() for f in result.findings])

    # From here on every answer is generated, not replayed. A cached completion costs
    # nothing and is reported as 0 tokens, so a repeated run would claim a token cost
    # that never happened. Excavation above may reuse its cache; it is not scored.
    llm.read_cache = False

    if "join_inference" in ablate:
        model.relationships = [
            r for r in model.relationships
            if (r.provenance.detector if r.provenance else "") == "procmine"
        ]

    from stratum.ask.cache import SemanticCache

    executor = ReadOnlyExecutor(connector, connector.profile)
    return AskContext(
        model=model,
        executor=executor,
        llm=llm,
        cost_guard=None if "no_cost_guard" in ablate else CostGuard(connector),
        verified=VerifiedLibrary(store.verified_path(database)),
        # The cache is deliberately absent during an eval: reusing an earlier answer
        # would measure the cache rather than the pipeline.
        cache=None if "value_index" not in ablate else SemanticCache(),
    )


def run_question(context, question, *, mode: str, ablate: set[str]) -> dict[str, Any]:
    """Ask one question and return the raw answer plus how it was asked."""
    from stratum.ask.pipeline import ask

    # The simulated user (BIRD-Interact style): answers only with the annotated
    # clarification and refuses anything else. Passed up front here rather than
    # round-tripping, because the pipeline asks for it at most once.
    clarification = ""
    if question.ambiguity:
        clarification = question.ambiguity.get("clarification", "")

    answer = ask(
        context,
        question.question,
        mode="default" if mode != "thorough" else "thorough",
        clarification=clarification,
        token_limit=6000,
    )
    return answer.to_dict()


def naive_baseline(context, question) -> dict[str, Any]:
    """The baseline every report is measured against (PLAN.md 3.4).

    Dump the whole physical schema into one prompt and ask for SQL. No semantic layer,
    no linking, no join graph, no compiler. This is what "just point an LLM at the
    database" produces, and it is the number the rest of the system has to beat.
    """
    from stratum.ask.generate import clean_sql
    from stratum.connectors.executor import ExecutionError

    schema_lines: list[str] = []
    for table in context.executor.connector.list_tables():
        if table.kind == "view":
            continue
        columns = [
            c for c in context.executor.connector.list_columns()
            if c.table == table.name
        ]
        cols = ", ".join(f"{c.name} {c.data_type}" for c in columns)
        schema_lines.append(f"{table.name}({cols})")

    # Named for the database actually connected, so the baseline is not handicapped by
    # being told to write the wrong dialect.
    dialect = {"postgres": "PostgreSQL", "sqlite": "SQLite", "mssql": "SQL Server"}.get(
        context.executor.connector.dialect, "SQL"
    )
    prompt = (
        "You are given the complete schema of a database. Answer the question with "
        f"one {dialect} SELECT statement. Return only SQL.\n\n"
        "SCHEMA\n" + "\n".join(schema_lines) + f"\n\nQUESTION\n{question.question}\n"
    )
    started = time.perf_counter()
    try:
        completion = context.llm.complete(
            [{"role": "user", "content": prompt}],
            role="large", stage="naive", max_tokens=500,
        )
    except Exception as exc:
        return {
            "status": "error", "message": str(exc), "physical_sql": "",
            "rows": [], "columns": [], "tokens": 0,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    sql = clean_sql(completion.text)
    out: dict[str, Any] = {
        "status": "ok", "physical_sql": sql, "logical_sql": sql,
        "tokens": completion.total_tokens,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "rows": [], "columns": [], "confidence": None, "entities": [],
        "assumptions": [], "repairs": 0,
    }
    try:
        result = context.executor.run(sql)
        out["columns"], out["rows"] = result.columns, [list(r) for r in result.rows]
        out["row_count"] = result.row_count
    except (ExecutionError, Exception) as exc:
        out["status"] = "error"
        out["message"] = str(exc)[:300]
    return out


def score_answer(question, answer: dict[str, Any], run_clean) -> QuestionOutcome:
    """Compare one answer against gold, or against the behaviour the question expects."""
    outcome = QuestionOutcome(
        id=question.id,
        tags=question.tags,
        difficulty=question.difficulty,
        correct=False,
        tokens=answer.get("tokens", 0),
        latency_ms=answer.get("elapsed_ms", 0),
        verified_hit=answer.get("verified_hit", False),
        confidence=(answer.get("confidence") or {}).get("score", 0.0),
        sql=answer.get("physical_sql", ""),
        abstained=answer.get("status") == "abstain",
        cost_guard=answer.get("status") == "cost_guard",
        clarified=bool(answer.get("clarification")),
    )

    # Behavioural questions are scored on what the system did, not on rows.
    if question.expect == "cost_guard":
        outcome.correct = outcome.cost_guard
        outcome.reason = "" if outcome.correct else f"expected the cost guard, got {answer.get('status')}"
        return outcome
    if question.expect == "abstain":
        outcome.correct = outcome.abstained
        outcome.reason = "" if outcome.correct else f"expected an abstention, got {answer.get('status')}"
        return outcome

    if answer.get("status") not in ("ok",):
        outcome.reason = f"{answer.get('status')}: {answer.get('message', '')[:140]}"
        return outcome

    try:
        expected = run_clean(question.gold_sql)
    except Exception as exc:  # sqlite3.Error or psycopg.Error
        outcome.reason = f"gold SQL failed: {exc}"
        return outcome

    actual = [tuple(r) for r in answer.get("rows", [])]
    match = compare(expected, actual, question.order_sensitive)
    outcome.correct = match.correct
    outcome.reason = match.reason
    return outcome


# Phrases the provider rotation uses when it never reached a model at all. An answer
# carrying one of these measures Groq's availability, not Stratum's accuracy, so a
# resumed run retries the question instead of scoring the outage.
LINE_END = chr(10)

_OUTAGE_MARKERS = (
    "every provider failed",
    "rate limited",
    "no provider configured",
)


# Failures that are the system's own, even when the client's wrapper message begins
# "every provider failed". A model that spends its whole output budget reasoning did
# answer the request — the pipeline gave that stage too few tokens. Retrying it until
# a lucky sample fits would launder a real failure into a pass.
_SYSTEM_FAILURE_MARKERS = (
    "reasoning and returned nothing",
    "could not parse",
)


def _is_infrastructure_failure(answer: dict[str, Any]) -> bool:
    # The full pipeline abstains on an outage; the naive baseline has no abstention
    # path and reports it as an error. Either way it is Groq's availability, not an
    # answer, and must be retried rather than scored.
    if answer.get("status") not in ("abstain", "error"):
        return False
    message = (answer.get("message") or "").lower()
    if any(marker in message for marker in _SYSTEM_FAILURE_MARKERS):
        return False
    return any(marker in message for marker in _OUTAGE_MARKERS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="chinook_v1")
    parser.add_argument(
        "--db", default=None,
        help="override the system-under-test URL; resolved from the suite otherwise",
    )
    parser.add_argument(
        "--providers", default=None,
        help=("comma-separated provider names to pin the run to (see providers.yaml). "
              "A comparison between two runs is only a comparison if they used the same "
              "model, and without a pin a rate limit silently changes it mid-run."),
    )
    parser.add_argument("--mode", choices=["full", "naive", "thorough"], default="full")
    parser.add_argument("--ablate", action="append", default=[], choices=list(ABLATIONS))
    parser.add_argument("--limit", type=int, help="only run the first N questions")
    parser.add_argument("--only", help="comma-separated question ids")
    parser.add_argument(
        "--pace", type=float, default=0.0, metavar="SECONDS",
        help=(
            "wait this long between questions. A free tier answers a burst with a "
            "penalty far longer than the window it protects, so spacing the run out "
            "finishes it faster than letting it trip the limit."
        ),
    )
    parser.add_argument("--resume", action="store_true",
                        help="skip questions already recorded in the run file")
    parser.add_argument("--offline", action="store_true",
                        help="do not call an LLM; exercises the deterministic half")
    parser.add_argument("--patience", type=float, default=150.0,
                        help="seconds to wait out a provider rate limit before giving up")
    parser.add_argument("--out", type=Path, default=HERE / "reports")
    args = parser.parse_args()

    suite = Suite.load(HERE / "suites" / f"{args.suite}.yaml")
    ablate = set(args.ablate)
    args.out.mkdir(parents=True, exist_ok=True)
    # The tag keys the report files, so an offline smoke run must not overwrite a
    # real one, and neither must an ablation.
    tag = args.mode
    if args.offline:
        tag += "+offline"
    if ablate:
        tag += "+" + "+".join(sorted(ablate))
    raw_path = args.out / f"{suite.name}.{tag}.answers.jsonl"

    done: dict[str, dict[str, Any]] = {}
    if args.resume and raw_path.exists():
        retryable = 0
        for line in raw_path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if _is_infrastructure_failure(record.get("answer") or {}):
                # Not a result. The system never got to answer, so recording it as an
                # abstention would score a quota outage as a property of the system.
                retryable += 1
                continue
            done[record["id"]] = record
        print(f"resuming: {len(done)} answers recorded, {retryable} to retry")

    questions = suite.questions
    if args.only:
        wanted = {q.strip() for q in args.only.split(",")}
        questions = [q for q in questions if q.id in wanted]
    if args.limit:
        questions = questions[: args.limit]

    from gauntlet.databases import clean_runner, legacy_url

    pinned = [p.strip() for p in args.providers.split(",")] if args.providers else None
    context = load_context(
        args.db or legacy_url(suite.database, suite.dialect), suite.database,
        ablate=ablate, offline=args.offline, patience=args.patience, providers=pinned,
    )
    run_clean = clean_runner(suite.clean, suite.dialect)

    outcomes: list[QuestionOutcome] = []
    mode_flag = "w" if args.resume else "a"
    with raw_path.open(mode_flag, encoding="utf-8") as sink:
        for index, question in enumerate(questions, 1):
            if args.pace and outcomes and question.id not in done:
                time.sleep(args.pace)

            if question.id in done:
                answer = done[question.id]["answer"]
                if args.resume:
                    sink.write(
                        json.dumps({"id": question.id, "answer": answer}, default=str)
                        + LINE_END
                    )
                    sink.flush()
            else:
                if args.mode == "naive":
                    answer = naive_baseline(context, question)
                else:
                    answer = run_question(
                        context, question, mode=args.mode, ablate=ablate
                    )
                sink.write(
                    json.dumps({"id": question.id, "answer": answer}, default=str) + "\n"
                )
                sink.flush()

            outcome = score_answer(question, answer, run_clean)
            outcomes.append(outcome)
            flag = "ok  " if outcome.correct else "FAIL"
            # Flushed explicitly: a suite can take half an hour against a free tier,
            # and an unobservable run looks indistinguishable from a hung one.
            print(
                f"  [{index:>3}/{len(questions)}] {flag} {question.id} "
                f"{outcome.tokens:>5}tok  {question.question[:52]}"
                + (f"   {outcome.reason[:60]}" if not outcome.correct else ""),
                flush=True,
            )

    report = score_run(suite, None, outcomes)
    report["mode"] = args.mode
    report["offline"] = args.offline
    # Which model actually answered, counted from the calls made rather than read from
    # config: the only honest label for a number is the model that produced it.
    from collections import Counter

    served = Counter(
        f"{c.provider}:{c.model}" for c in context.llm.calls if c.ok and c.stage != "enrich"
    )
    report["models"] = dict(served.most_common())
    report["providers_pinned"] = pinned or []

    # Fit the confidence weights on this run and measure whether the score means
    # anything. Done here rather than as a separate command because the scorer is the
    # only thing allowed to decide what "correct" means, and it has just decided.
    if args.mode != "naive":
        from stratum.ask.calibration import (
            fit,
            observations_from_run,
            render_reliability,
        )

        observations = observations_from_run(
            raw_path, {o.id: o.correct for o in outcomes}
        )
        calibration = fit(observations)
        report["calibration"] = calibration.to_dict()
        calibration.save(args.out / f"{suite.name}.{tag}.calibration.json")
        print()
        print(render_reliability(calibration))
    report["ablations"] = sorted(ablate)

    report_path = args.out / f"{suite.name}.{tag}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), "utf-8")
    markdown = render_report(report)
    (args.out / f"{suite.name}.{tag}.md").write_text(markdown, "utf-8")

    # The per-question file and knowledge recall the web benchmark view serves. Written
    # here, on the side allowed to read gold answers and the manifest, so the engine's
    # API never has to.
    from gauntlet.publish import publish_recall, publish_run

    publish_run(suite.name, tag)
    publish_recall(suite.name)

    print()
    print(markdown)
    print(f"\nwrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
