"""Intervention: re-ask a failed question but hand Stratum the entities a correct answer needs.
If it now answers correctly, the failure was retrieval; if not, it was generation."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from stratum.ask.cache import SemanticCache  # noqa: F401
from stratum.ask.pipeline import AskContext, ask
from stratum.config import load_env, settings
from stratum.connectors.costguard import CostGuard
from stratum.connectors.executor import ReadOnlyExecutor
from stratum.connectors.registry import build
from stratum.llm.client import LLMClient
from stratum.semantic.store import ModelStore
from stratum.semantic.verified import VerifiedLibrary

from gauntlet.databases import clean_runner
from gauntlet.scorer import Suite, compare

load_env(ROOT)
settings()
DB = "chinook_legacy"
store = ModelStore(ROOT / "engine" / ".stratum" / "models")
model = store.load(DB, 1)  # the model that reproduces every recorded answer
conn = build("sqlite:///" + str((ROOT / "gauntlet" / "build" / "chinook_legacy.sqlite").resolve()))
conn.connect()
executor = ReadOnlyExecutor(conn, conn.profile)
llm = LLMClient(cache_dir=store.cache_dir(DB), offline=False, max_wait_s=150, only=["groq"])
llm.read_cache = False  # every answer is generated, never replayed
context = AskContext(
    model=model, executor=executor, llm=llm, cost_guard=CostGuard(conn),
    verified=VerifiedLibrary(store.verified_path(DB)), cache=None,
)
suite = {q.id: q for q in Suite.load(ROOT / "gauntlet" / "suites" / "chinook_v1.yaml").questions}
run_clean = clean_runner("chinook_clean", "sqlite")

ENTITIES = json.loads(Path(Path(__file__).parent / "chinook_v1.entities.json").read_text("utf-8"))
only = sys.argv[2:] or list(ENTITIES)
out_path = Path(sys.argv[1])
results = json.loads(Path(out_path).read_text("utf-8")) if out_path.exists() else {}

for qid in only:
    q = suite[qid]
    clarification = (q.ambiguity or {}).get("clarification", "") if q.ambiguity else ""
    answer = ask(context, q.question, mode="default", clarification=clarification,
                 approved_entities=ENTITIES[qid], token_limit=6000)
    gold = run_clean(q.gold_sql)
    rows = [tuple(r) for r in answer.rows] if answer.status == "ok" else []
    verdict = compare(gold, rows, q.order_sensitive) if answer.status == "ok" else None
    ok = bool(verdict and verdict.correct)
    results[qid] = {
        "given_entities": ENTITIES[qid], "status": answer.status, "correct": ok,
        "reason": (verdict.reason if verdict else answer.message)[:160],
        "logical_sql": answer.logical_sql, "tokens": answer.tokens,
    }
    print(f"{qid}: {'CORRECT' if ok else 'wrong  '} status={answer.status} tokens={answer.tokens} | {results[qid]['reason']}")
    print("      ", " ".join((answer.logical_sql or "").split())[:240])
    out_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), "utf-8")
