"""Score a recorded run a second way, without asking a model anything.

The strict score is the headline and is never replaced. This reads the answers a finished
run already recorded and compares them again, so the two numbers can sit side by side:

* **strict**: the benchmark's own rule, order-sensitive where the question says so
* **tie-aware**: the same, except that ordering ties differently is not an error

    python -m gauntlet.rescore --suite chinook_v1 --tag full
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "engine"))

from gauntlet.databases import clean_runner  # noqa: E402
from gauntlet.runner import score_answer  # noqa: E402
from gauntlet.scorer import Suite  # noqa: E402

REPORTS = HERE / "reports"


def rescore(suite_name: str, tag: str) -> dict:
    suite = Suite.load(HERE / "suites" / f"{suite_name}.yaml")
    answers: dict[str, dict] = {}
    for line in (REPORTS / f"{suite_name}.{tag}.answers.jsonl").read_text("utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            answers[record["id"]] = record["answer"]       # a resumed run keeps the last

    dialect = "postgres" if "pagila" in suite.database else "sqlite"
    run_clean = clean_runner(suite.clean, dialect)
    executable = [q for q in suite.questions if q.is_executable]
    strict: dict[str, bool] = {}
    relaxed: dict[str, bool] = {}
    for q in executable:
        answer = answers.get(q.id)
        if answer is None:
            continue
        strict[q.id] = score_answer(q, answer, run_clean).correct
        relaxed[q.id] = score_answer(q, answer, run_clean, tie_aware=True).correct

    gained = sorted(q for q in relaxed if relaxed[q] and not strict[q])
    return {
        "suite": suite_name,
        "tag": tag,
        "questions": len(strict),
        "strict_correct": sum(strict.values()),
        "strict": round(sum(strict.values()) / len(strict), 3),
        "tie_aware_correct": sum(relaxed.values()),
        "tie_aware": round(sum(relaxed.values()) / len(relaxed), 3),
        "accepted_only_when_ties_are_not_penalised": gained,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="gauntlet.rescore")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--tag", default="full")
    args = parser.parse_args()
    report = rescore(args.suite, args.tag)
    out = REPORTS / f"{args.suite}.{args.tag}.tie_aware.json"
    out.write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
