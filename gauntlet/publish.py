"""Write what the benchmark view needs, from the side allowed to know the answers.

The web Lab shows each question's gold answer next to the system's, and how much of the
answer key excavation recovered. Both need things engine code is forbidden to read —
gold SQL run against the clean database, and `manifest.json` — so the engine API does
not compute them. This module does, and writes plain files into `gauntlet/reports/`
that the API serves as they are:

    <suite>.<tag>.questions.json   one row per question: outcome, gold and ours
    <suite>.<tag>.json             the summary, rebuilt for runs that stopped partway
    <suite>.recall.json            knowledge recall of the current model

The runner calls `publish_run` at the end of every run. For runs that already exist:

    python -m gauntlet.publish
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
sys.path.insert(0, str(HERE.parent / "engine"))

# How much of each result goes into the file: enough to see why an answer differs,
# not a copy of the table.
PREVIEW_ROWS = 12


def _preview(columns: list[str], rows: list[Any]) -> dict[str, Any]:
    return {
        "columns": [str(c) for c in columns],
        "rows": [[_cell(v) for v in row] for row in rows[:PREVIEW_ROWS]],
        "total": len(rows),
    }


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def publish_run(suite_name: str, tag: str) -> Path | None:
    """Per-question outcomes for one run, and its summary if it has none."""
    from gauntlet.databases import clean_query, clean_runner
    from gauntlet.runner import _is_infrastructure_failure, score_answer
    from gauntlet.scorer import Suite, render_report, score_run

    answers_path = REPORTS / f"{suite_name}.{tag}.answers.jsonl"
    if not answers_path.exists():
        return None
    suite = Suite.load(HERE / "suites" / f"{suite_name}.yaml")
    answers = {}
    for line in answers_path.read_text("utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            answers[record["id"]] = record["answer"]

    run_clean = clean_runner(suite.clean, suite.dialect)
    query_clean = clean_query(suite.clean, suite.dialect)
    rows: list[dict[str, Any]] = []
    outcomes = []
    for question in suite.questions:
        answer = answers.get(question.id)
        row: dict[str, Any] = {
            "id": question.id,
            "question": question.question,
            "tags": question.tags,
            "difficulty": question.difficulty,
            "expect": question.expect,
            "answered": answer is not None and not _is_infrastructure_failure(answer),
        }
        if question.gold_sql:
            try:
                columns, gold = query_clean(question.gold_sql)
                row["gold"] = _preview(columns, gold)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                row["gold"] = {"error": str(exc)[:200]}
            row["gold_sql"] = question.gold_sql.strip()
        if row["answered"]:
            outcome = score_answer(question, answer, run_clean)
            outcomes.append(outcome)
            row.update({
                "correct": outcome.correct,
                "reason": outcome.reason,
                "status": answer.get("status"),
                "tokens": answer.get("tokens", 0),
                "confidence": (answer.get("confidence") or {}).get("score"),
                "logical_sql": answer.get("logical_sql", ""),
                "physical_sql": answer.get("physical_sql", ""),
                "message": answer.get("message", ""),
                "assumptions": [a.get("text", "") for a in answer.get("assumptions") or []],
                "ours": _preview(answer.get("columns") or [], answer.get("rows") or []),
            })
        rows.append(row)

    out = REPORTS / f"{suite_name}.{tag}.questions.json"
    out.write_text(json.dumps(rows, indent=1, default=str), "utf-8")

    # A run stopped partway (a daily limit, a killed process) never reached the point
    # where the runner writes its summary. Rebuild it from what was answered, marked as
    # partial so nothing downstream mistakes it for a finished run.
    summary_path = REPORTS / f"{suite_name}.{tag}.json"
    if outcomes and not summary_path.exists():
        report = score_run(suite, None, outcomes)
        report["mode"] = tag.split("+")[0]
        report["partial"] = True
        report["questions_answered"] = len(outcomes)
        summary_path.write_text(json.dumps(report, indent=2, default=str), "utf-8")
        (REPORTS / f"{suite_name}.{tag}.md").write_text(render_report(report), "utf-8")
    return out


def publish_recall(suite_name: str) -> Path | None:
    """How much of the answer key the current excavated model recovered."""
    from gauntlet.databases import manifest_path
    from gauntlet.scorer import Suite, knowledge_recall
    from stratum.semantic.store import ModelStore

    suite = Suite.load(HERE / "suites" / f"{suite_name}.yaml")
    manifest = manifest_path(suite.database)
    model = ModelStore(HERE.parent / "engine" / ".stratum" / "models").load(suite.database)
    if model is None or not manifest.exists():
        return None
    scores = knowledge_recall(manifest, model.model_dump())
    out = REPORTS / f"{suite_name}.recall.json"
    out.write_text(json.dumps(scores, indent=2), "utf-8")
    return out


def main() -> int:
    for answers in sorted(REPORTS.glob("*.answers.jsonl")):
        stem = answers.name[: -len(".answers.jsonl")]
        if "." not in stem or stem.startswith("archive"):
            continue
        suite_name, tag = stem.split(".", 1)
        written = publish_run(suite_name, tag)
        print(f"{stem}: {written.name if written else 'skipped'}")
    for suite in sorted({p.name.split(".")[0] for p in REPORTS.glob("*.answers.jsonl")}):
        if suite.startswith("archive"):
            continue
        written = publish_recall(suite)
        print(f"{suite} recall: {written.name if written else 'no model or manifest'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
