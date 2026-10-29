import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
S = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from stratum.ask.compiler import compile_logical
from stratum.connectors.executor import ReadOnlyExecutor
from stratum.connectors.registry import build
from stratum.semantic.store import ModelStore

from gauntlet.databases import clean_runner
from gauntlet.scorer import Suite, compare

model = ModelStore(ROOT / "engine/.stratum/models").load("chinook_legacy", 1)
conn = build("sqlite:///" + str((ROOT / "gauntlet/build/chinook_legacy.sqlite").resolve()))
conn.connect()
ex = ReadOnlyExecutor(conn, conn.profile)
clean = clean_runner("chinook_clean", "sqlite")
suite = {q.id: q for q in Suite.load(ROOT / "gauntlet/suites/chinook_v1.yaml").questions}
oracles = json.loads(Path(S / "chinook_v1.correct_queries.json").read_text("utf-8"))
raw = {}
for line in Path(ROOT / "gauntlet/reports/chinook_v1.full.answers.jsonl").read_text("utf-8").splitlines():
    r = json.loads(line)
    raw[r["id"]] = r["answer"]
samples = [json.loads(Path(S / f).read_text("utf-8")) for f in ("chinook_v1.intervention.sample1.json", "chinook_v1.intervention.sample2.json", "chinook_v1.intervention.sample3.json")]


def run(sql):
    return [tuple(r) for r in ex.run(compile_logical(sql, model, conn.profile).physical_sql).rows]


def judge(qid, sql):
    """strict = the benchmark's own scoring; substance = would the answer be right if the scorer
    asked only for what the question asks (order ignored, count column optional where gold adds one)."""
    q, gold = suite[qid], clean(suite[qid].gold_sql)
    rows = run(sql)
    strict = compare(gold, rows, q.order_sensitive).correct
    lenient = compare(gold, rows, False).correct
    if not lenient and qid in ("c-011", "c-040"):
        lenient = compare([(g[0],) for g in gold], [(r[0],) for r in rows], False).correct
    return strict, lenient


CLASS = {
    "c-005": ("excavation", "", "The model exposes 33 country spellings where the clean data has 24: 'USA' and 'U.S.A.' stay separate. A correct query over the model returns 420.07 against gold's 523.06."),
    "c-013": ("excavation", "retrieval", "customer.country_code carries four US spellings (U.S.A., US, USA, usa). A correct query over the model counts 5 customers against gold's 13. Stratum also bound the literal 'USA' to billing_country rather than the customer's country."),
    "c-036": ("excavation", "", "Same unmerged country spellings: 33 groups against 24."),
    "c-021": ("compilation", "", "The model's logical SQL was a valid anti-join. The compiler pushed `album.id IS NULL` into the left join's right-hand side, so every artist looked album-less: 275 rows against 71. The equivalent NOT EXISTS passes."),
    "c-006": ("benchmark", "", "Returns exactly the same ten customers as gold. They differ only in the order of customers with tied totals (Barnett and Zimmermann, 43.62), under an order-sensitive scorer. Passes when order is ignored."),
    "c-042": ("benchmark", "", "Same as c-006: the same ten customers, tied totals in a different order."),
    "c-025": ("benchmark", "", "At least five cities tie at two customers (Berlin, London, Mountain View, Paris, Prague). Gold picks Berlin by alphabetical tie-break; the question has no unique answer."),
    "c-012": ("benchmark", "", "The legacifier replaced the real composer of 74 tracks with a blank or marker (clean data has 'AC/DC', 'Billy Cobham', ...). Stratum counts 1,051 blank composers; gold's 977 cannot be recovered from the legacy data by any system."),
    "c-010": ("retrieval", "", "'Led Zeppelin' was not bound to artist.name and `artist` was not linked, so the model matched the album title instead."),
    "c-019": ("retrieval", "", "Only sales_order_line was linked (it has a column called unit_price); the track table `product` was not."),
    "c-023": ("retrieval", "excavation (naming)", "The track table is named `product`, so 'tracks' linked to playlist_track and the join plan was rooted there, multiplying counts by playlist membership."),
    "c-030": ("retrieval", "excavation (naming)", "Same: rooted at playlist_track."),
    "c-038": ("retrieval", "excavation (naming)", "Same: rooted at playlist_track."),
    "c-011": ("retrieval", "excavation (naming); benchmark (latent)", "Same root cause. With the right entities Stratum answers 'Rock' correctly but omits the count column that gold includes, so the scorer still marks it wrong."),
    "c-040": ("retrieval", "excavation (naming); benchmark (latent)", "Same as c-011: 'Iron Maiden' is right, the count column is missing."),
    "c-045": ("retrieval", "benchmark (latent)", "Noise entities (playlist_track, product, album) led to a line-level join: 200 rows against 80. With billing alone the 80 rows, dates and totals match gold; only the ordering (descending against gold's ascending) differs."),
    "c-026": ("generation", "", "The right entities were linked in the original run. The model sums per-order header totals across order lines, which multiplies revenue. Wrong in 2 of 4 attempts with the right entities."),
    "c-028": ("generation", "", "The right entities were linked. The model groups by order_number without store_code, though order numbers restart per store (a composite key). Wrong in every valid attempt."),
}

records = []
for qid, (primary, secondary, evidence) in CLASS.items():
    q = suite[qid]
    ok = True
    try:
        run(oracles[qid])
        o_strict, o_lenient = judge(qid, oracles[qid])
    except Exception:  # noqa: BLE001
        o_strict = o_lenient = False
    iv = []
    for s in samples:
        item = s.get(qid)
        if not item or item["status"] != "ok":
            continue  # a rate-limit abstention is not an answer
        strict, lenient = judge(qid, item["logical_sql"])
        iv.append({"strict": strict, "substance": lenient, "sql": " ".join(item["logical_sql"].split())})
    records.append({
        "id": qid, "question": q.question, "tags": q.tags, "primary_cause": primary,
        "also": secondary, "evidence": evidence,
        "original": {"linked_entities": raw[qid]["entities"], "join_plan_root": (raw[qid].get("join_plan") or {}).get("root")},
        "counterfactual_correct_query": {"logical_sql": oracles[qid], "matches_gold": o_strict, "matches_gold_ignoring_order": o_lenient},
        "intervention_given_correct_entities": {
            "entities": json.loads(Path(S / "chinook_v1.entities.json").read_text("utf-8")).get(qid),
            "valid_samples": len(iv), "correct_under_benchmark_scoring": sum(i["strict"] for i in iv),
            "correct_in_substance": sum(i["substance"] for i in iv),
            "samples": iv,
        } if iv else None,
    })

by = {}
for r in records:
    by[r["primary_cause"]] = by.get(r["primary_cause"], 0) + 1
out = {
    "suite": "chinook_v1", "scored_failures": len(records), "model": "openai/gpt-oss-120b (Groq)",
    "semantic_model": "chinook_legacy model.v1 (reproduces all 48 recorded physical queries exactly)",
    "by_primary_cause": by, "failures": records,
}
(ROOT / "gauntlet/reports/chinook_v1.attribution.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), "utf-8")
print(json.dumps(by))
for r in records:
    iv = r["intervention_given_correct_entities"]
    print(f"{r['id']}  {r['primary_cause']:<11} oracle={'PASS' if r['counterfactual_correct_query']['matches_gold'] else 'FAIL'}"
          + (f"  intervention: {iv['correct_under_benchmark_scoring']}/{iv['valid_samples']} strict, {iv['correct_in_substance']}/{iv['valid_samples']} substance" if iv else ""))
