"""Counterfactual for each Stratum failure: would a *correct* logical query, written against
Stratum's own semantic model and compiled by Stratum's own compiler, have matched gold?"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from stratum.ask.compiler import compile_logical
from stratum.connectors.executor import ReadOnlyExecutor
from stratum.connectors.registry import build
from stratum.semantic.store import ModelStore

from gauntlet.databases import clean_runner
from gauntlet.scorer import Suite, compare

store = ModelStore(ROOT / "engine" / ".stratum" / "models")
model = store.load("chinook_legacy", 1)  # the version that reproduces every recorded answer
conn = build("sqlite:///" + str((ROOT / "gauntlet" / "build" / "chinook_legacy.sqlite").resolve()))
conn.connect()
executor = ReadOnlyExecutor(conn, conn.profile)
suite = Suite.load(ROOT / "gauntlet" / "suites" / "chinook_v1.yaml")
questions = {q.id: q for q in suite.questions}
run_clean = clean_runner("chinook_clean", "sqlite")

ORACLES = json.loads(Path(Path(__file__).parent / "chinook_v1.correct_queries.json").read_text("utf-8"))


def run_oracle(qid: str, logical: str):
    q = questions[qid]
    gold = run_clean(q.gold_sql)
    try:
        physical = compile_logical(logical, model, conn.profile).physical_sql
        result = executor.run(physical)
        rows = [tuple(r) for r in result.rows]
    except Exception as exc:  # noqa: BLE001
        return False, f"ERROR {str(exc)[:160]}", gold, [], ""
    m = compare(gold, rows, q.order_sensitive)
    return m.correct, m.reason, gold, rows, physical


if __name__ == "__main__":
    only = sys.argv[1:] or list(ORACLES)
    for qid in only:
        ok, reason, gold, rows, physical = run_oracle(qid, ORACLES[qid])
        print(f"{qid}: {'PASS' if ok else 'FAIL'}  {reason}")
        if not ok:
            print("   gold:", json.dumps(gold[:4], default=str)[:200], f"({len(gold)} rows)")
            print("   ours:", json.dumps(rows[:4], default=str)[:200], f"({len(rows)} rows)")
