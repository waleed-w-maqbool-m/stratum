"""Gauntlet scoring.

Three things live here:

* `check_gold` — runs both gold formulations of every question against the clean
  database and fails if they disagree. Public benchmarks have been shown to carry
  annotation errors; this catches ours before they become "accuracy".
* `score_run` — execution accuracy of the system's SQL (run on the *legacy* copy)
  against gold (run on the *clean* copy), compared as multisets, Spider 2.0-style
  column matching.
* `knowledge_recall` — how much of the manifest the excavation pipeline rediscovered.

**This is the only module permitted to read `manifest.json`.** Engine code that reads
it is cheating; see CLAUDE.md non-negotiable #3.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- normalising


def normalise(value: Any) -> Any:
    """Numbers to 2 dp, dates to ISO, strings trimmed and casefolded. Nothing else."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return round(value + 0.0, 2)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value
    text = str(value).strip()
    # A date-looking string collapses to its ISO date part so that '2011-01-03 00:00:00'
    # and '2011-01-03' compare equal.
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        head = text[:10]
        try:
            datetime.strptime(head, "%Y-%m-%d")
            return head
        except ValueError:
            pass
    try:
        return round(float(text), 2)
    except ValueError:
        return text.casefold()


def _column_vectors(rows: list[tuple]) -> list[tuple]:
    """Transpose rows into per-column value vectors, each as a sorted multiset."""
    if not rows:
        return []
    width = len(rows[0])
    return [tuple(sorted(str(r[i]) for r in rows)) for i in range(width)]


@dataclass
class MatchResult:
    correct: bool
    reason: str = ""
    matched_columns: int = 0
    gold_columns: int = 0


def compare(
    gold_rows: list[tuple],
    system_rows: list[tuple],
    order_sensitive: bool = False,
) -> MatchResult:
    """Spider 2.0-style comparison.

    Every gold column must be matched by some system column with the same value
    vector. Extra system columns are allowed — a query that returns the answer plus
    a helpful label is not wrong.
    """
    gold = [tuple(normalise(v) for v in row) for row in gold_rows]
    system = [tuple(normalise(v) for v in row) for row in system_rows]

    if not gold and not system:
        return MatchResult(True, "both empty")
    if len(gold) != len(system):
        return MatchResult(
            False, f"row count differs: gold {len(gold)} vs system {len(system)}"
        )

    if order_sensitive:
        gold_cols = [tuple(r[i] for r in gold) for i in range(len(gold[0]))]
        sys_cols = [tuple(r[i] for r in system) for i in range(len(system[0]))]
    else:
        gold_cols = _column_vectors(gold)
        sys_cols = _column_vectors(system)

    if not gold_cols:
        return MatchResult(True, "no columns")

    unused = list(range(len(sys_cols)))
    matched = 0
    # Which system column answered each gold column, in gold's order. The alignment
    # check below is entitled to look at these and nothing else.
    matched_indices: list[int] = []
    for gcol in gold_cols:
        hit = next((i for i in unused if sys_cols[i] == gcol), None)
        if hit is None:
            return MatchResult(
                False,
                f"no system column matches a gold column ({matched}/{len(gold_cols)} matched)",
                matched,
                len(gold_cols),
            )
        unused.remove(hit)
        matched_indices.append(hit)
        matched += 1

    # Column-wise matching alone can accept a result whose values are right column by
    # column but paired wrongly across rows, so confirm the row multiset agrees too.
    #
    # Only over the matched columns. Comparing whole rows would measure gold's columns
    # against every column the system returned, which contradicts the rule above that
    # extra columns are allowed: a correct answer that also selects an id would fail
    # here having matched every gold column exactly.
    if not order_sensitive:
        projected = [tuple(row[i] for i in matched_indices) for row in system]
        if Counter(map(str, gold)) != Counter(map(str, projected)):
            return MatchResult(False, "columns match individually but rows do not align",
                               matched, len(gold_cols))
    return MatchResult(True, "", matched, len(gold_cols))


# ------------------------------------------------------------------------------ suites


@dataclass
class Question:
    id: str
    question: str
    tags: list[str] = field(default_factory=list)
    difficulty: str = "medium"
    gold_sql: str | None = None
    gold_sql_alt: str | None = None
    order_sensitive: bool = False
    ambiguity: dict[str, Any] | None = None
    expect: str | None = None       # None | "cost_guard" | "abstain"
    allow_empty: bool = False       # opt in when an empty result is the real answer

    @property
    def is_executable(self) -> bool:
        return bool(self.gold_sql)


@dataclass
class Suite:
    name: str
    database: str
    clean: str
    dialect: str
    questions: list[Question]

    @classmethod
    def load(cls, path: Path) -> Suite:
        raw = yaml.safe_load(path.read_text("utf-8"))
        return cls(
            name=raw["suite"],
            database=raw["database"],
            clean=raw["clean"],
            dialect=raw.get("dialect", "sqlite"),
            questions=[Question(**q) for q in raw["questions"]],
        )

    def by_id(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.id == qid), None)


# ----------------------------------------------------------------------- gold checking


def run_sql(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def check_gold(suite: Suite, clean_db: Path | None = None, verbose: bool = False) -> int:
    """Both formulations of each question must agree. Returns the failure count.

    `clean_db` overrides where a SQLite suite's clean copy is; left out, the database is
    resolved from the suite itself, which is the only way that works for Postgres.
    """
    from gauntlet.databases import clean_runner

    if clean_db is not None:
        def run(sql: str) -> list[tuple]:
            return run_sql(clean_db, sql)
    else:
        run = clean_runner(suite.clean, suite.dialect)
    failures = 0
    checked = 0
    for q in suite.questions:
        if not q.is_executable:
            if q.expect is None:
                print(f"  FAIL   {q.id}: no gold SQL and no `expect` behaviour declared")
                failures += 1
            else:
                print(f"  skip   {q.id}: behavioural question (expect: {q.expect})")
            continue
        if not q.gold_sql_alt:
            print(f"  FAIL   {q.id}: missing the second gold formulation")
            failures += 1
            continue
        checked += 1
        try:
            primary = run(q.gold_sql)
            alt = run(q.gold_sql_alt)
        except Exception as exc:  # sqlite3.Error or psycopg.Error, whichever the suite runs on
            print(f"  ERROR  {q.id}: {exc}")
            failures += 1
            continue
        # Two formulations that both return nothing "agree" without testing anything.
        if not primary and not q.allow_empty:
            failures += 1
            print(f"  FAIL   {q.id}: gold returns no rows — the question tests nothing. "
                  f"Fix the question, or set allow_empty: true if empty is the answer.")
            continue
        result = compare(primary, alt, q.order_sensitive)
        if result.correct:
            print(f"  ok     {q.id}  ({len(primary)} rows)  {q.question[:56]}")
        else:
            failures += 1
            print(f"  FAIL   {q.id}: gold formulations disagree — {result.reason}")
            if verbose:
                print(f"         primary: {primary[:3]}")
                print(f"         alt:     {alt[:3]}")
    print(f"\n{checked - failures}/{checked} gold pairs agree ({len(suite.questions)} questions)")
    return failures


# -------------------------------------------------------------------- knowledge recall


def _prf(true_positives: int, predicted: int, actual: int) -> dict[str, float]:
    precision = true_positives / predicted if predicted else 0.0
    recall = true_positives / actual if actual else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def _bare(name: str) -> str:
    """`public.fin_txn` -> `fin_txn`. The manifest names tables without a schema."""
    return str(name or "").split(".")[-1]


def _comparable(model: dict[str, Any]) -> dict[str, Any]:
    """Put a model in the shape the answer key is written in.

    Two differences that only appeared with a second database: a model saved to disk
    keys its entities by name rather than listing them, and a Postgres catalog
    schema-qualifies every table (`public.fin_txn`) where the manifest does not. Scored
    as written, every pagila edge would count as missed.
    """
    out = dict(model)
    entities = model.get("entities", [])
    if isinstance(entities, dict):
        entities = list(entities.values())
    out["entities"] = [{**e, "source": _bare(e.get("source", ""))} for e in entities]
    out["relationships"] = [
        {**r, "from_table": _bare(r.get("from_table", "")), "to_table": _bare(r.get("to_table", ""))}
        for r in model.get("relationships", [])
    ]
    out["table_classes"] = {_bare(t): k for t, k in model.get("table_classes", {}).items()}
    for section in ("drift", "embedded_formats", "god_tables"):
        out[section] = [{**d, "table": _bare(d.get("table", ""))} for d in model.get(section, [])]
    out["enums"] = {
        ".".join([_bare(t.rsplit(".", 1)[0]), t.rsplit(".", 1)[-1]]) if "." in t else t: v
        for t, v in model.get("enums", {}).items()
    }
    return out


def declared_recall(
    manifest_path: Path, kept: list[tuple], model: dict[str, Any]
) -> dict[str, Any]:
    """On a database that kept some foreign keys: are the labels right?

    `kept` are the joins the benchmark declared as constraints, as the same
    (child, child cols, parent, parent cols) tuples `knowledge_recall` uses. Two claims
    are scored. Every kept key must be labelled `declared`, and the keys that were dropped
    must come back as `undeclared_fk` suggestions, with nothing else reported there. A
    kept key that shows up as undeclared would mean the declared constraint was ignored.
    """
    manifest = json.loads(manifest_path.read_text("utf-8"))

    def key(child: str, ccols: list[str], parent: str, pcols: list[str]) -> tuple:
        return (_bare(child).lower(), tuple(sorted(c.lower() for c in ccols)),
                _bare(parent).lower(), tuple(sorted(p.lower() for p in pcols)))

    def both_ways(keys: set[tuple]) -> set[tuple]:
        return keys | {(p, pc, c, cc) for (c, cc, p, pc) in keys}

    truth = {
        key(j["child"], j["child_cols"], j["parent"], j["parent_cols"])
        for j in manifest["joins"] if not j.get("condition")
    }
    kept_keys = {key(*k[0:1], list(k[1]), k[2], list(k[3])) for k in kept}
    dropped = truth - kept_keys

    declared = {
        key(r["from_table"], r["from_columns"], r["to_table"], r["to_columns"])
        for r in model.get("relationships", []) if r.get("origin") == "declared"
    }
    undeclared = {
        key(g["table"], g["columns"], g["to_table"], g["to_columns"])
        for g in model.get("gaps", []) if g["gap_type"] == "undeclared_fk"
    }

    def share(part: set[tuple], whole: set[tuple]) -> float:
        return round(len(part) / len(whole), 3) if whole else 0.0

    return {
        "declared_labelled_recall": share(
            {k for k in kept_keys if k in both_ways(declared)}, kept_keys
        ),
        "declared_labelled_precision": share(
            {d for d in declared if d in both_ways(kept_keys)}, declared
        ),
        "undeclared_gap_recall": share(
            {d for d in dropped if d in both_ways(undeclared)}, dropped
        ),
        "undeclared_gap_precision": share(
            {u for u in undeclared if u in both_ways(dropped)}, undeclared
        ),
        "kept_reported_as_undeclared": len(
            {u for u in undeclared if u in both_ways(kept_keys)}
        ),
        "undeclared_gaps_reported": len(undeclared),
        "undeclared_not_in_answer_key": sorted(
            f"{u[0]}{list(u[1])} -> {u[2]}{list(u[3])}"
            for u in undeclared if u not in both_ways(truth)
        ),
    }


def knowledge_recall(manifest_path: Path, model: dict[str, Any]) -> dict[str, Any]:
    """Compare the engine's semantic model against the answer key.

    `model` is the semantic model as JSON (the engine's own output). Nothing in this
    function is ever fed back into the engine.
    """
    manifest = json.loads(manifest_path.read_text("utf-8"))
    model = _comparable(model)
    scores: dict[str, Any] = {}

    # --- joins: an edge counts as found if the same (child table, child cols, parent
    #     table, parent cols) pair appears, regardless of how the condition is spelled.
    def join_key(child: str, ccols: list[str], parent: str, pcols: list[str]) -> tuple:
        return (child.lower(), tuple(sorted(c.lower() for c in ccols)),
                parent.lower(), tuple(sorted(p.lower() for p in pcols)))

    truth_joins = {
        join_key(j["child"], j["child_cols"], j["parent"], j["parent_cols"])
        for j in manifest["joins"]
    }
    found_joins = {
        join_key(r["from_table"], r["from_columns"], r["to_table"], r["to_columns"])
        for r in model.get("relationships", [])
    }
    # A join is symmetric for scoring: direction confusion is not a knowledge failure.
    truth_sym = truth_joins | {(p, pc, c, cc) for (c, cc, p, pc) in truth_joins}
    found_sym = found_joins | {(p, pc, c, cc) for (c, cc, p, pc) in found_joins}
    # Counted from each side separately: if the model emits an edge in both directions,
    # that is one truth edge recovered, not two, and recall must not exceed 1.
    matched_found = {j for j in found_joins if j in truth_sym}
    matched_truth = {j for j in truth_joins if j in found_sym}
    precision = len(matched_found) / len(found_joins) if found_joins else 0.0
    recall = len(matched_truth) / len(truth_joins) if truth_joins else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    scores["joins"] = {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    }

    # --- composite / logical keys
    truth_keys = {t.lower(): sorted(c.lower() for c in cols)
                  for t, cols in manifest["keys"].items()}
    found_keys = {e["source"].split(".")[-1].lower(): sorted(c.lower() for c in e.get("key", []))
                  for e in model.get("entities", [])}
    key_hits = sum(1 for t, cols in truth_keys.items() if found_keys.get(t) == cols)
    scores["keys"] = _prf(key_hits, len(found_keys), len(truth_keys))
    composite_truth = {t: c for t, c in truth_keys.items() if len(c) > 1}
    composite_hits = sum(1 for t, cols in composite_truth.items() if found_keys.get(t) == cols)
    scores["composite_keys"] = {
        "found": composite_hits,
        "total": len(composite_truth),
        "recall": round(composite_hits / len(composite_truth), 3) if composite_truth else 0.0,
    }

    # --- soft delete rules: recall on the table, not on the exact predicate text
    truth_sd = {t.lower() for t in manifest["soft_deletes"]}
    found_sd = {
        e["source"].split(".")[-1].lower()
        for e in model.get("entities", [])
        if e.get("default_filters")
    }
    scores["soft_deletes"] = _prf(len(truth_sd & found_sd), len(found_sd), len(truth_sd))

    # --- table classification (core / archive / staging / backup / dead)
    truth_class = {t.lower(): k for t, k in manifest["table_classes"].items()}
    found_class = {t.lower(): k for t, k in model.get("table_classes", {}).items()}
    # Binary F1 on "is this table worth retrieving": core vs everything else.
    truth_dead = {t for t, k in truth_class.items() if k != "core"}
    found_dead = {t for t, k in found_class.items() if k != "core"}
    scores["dead_tables"] = _prf(
        len(truth_dead & found_dead), len(found_dead), len(truth_dead)
    )
    agree = sum(1 for t, k in truth_class.items() if found_class.get(t) == k)
    scores["table_class_exact"] = {
        "agree": agree,
        "total": len(truth_class),
        "accuracy": round(agree / len(truth_class), 3) if truth_class else 0.0,
    }

    # --- enum decoding: a value is decoded correctly if the label matches on the
    #     first word, casefolded (labels are free text, spelling will vary).
    enum_hits = enum_total = 0
    found_enums = model.get("enums", {})
    for target, mapping in manifest["enums"].items():
        found = found_enums.get(target) or found_enums.get(target.lower()) or {}
        for value, label in mapping.items():
            enum_total += 1
            got = str(found.get(str(value), "")).casefold()
            if got and got.split()[0:1] == label.casefold().split()[0:1]:
                enum_hits += 1
    scores["enums"] = {
        "decoded": enum_hits,
        "total": enum_total,
        "accuracy": round(enum_hits / enum_total, 3) if enum_total else 0.0,
    }

    # --- drift detection
    truth_drift = {(d["table"].lower(), d["kind"]) for d in manifest["drift"]}
    found_drift = {(d.get("table", "").lower(), d.get("kind", ""))
                   for d in model.get("drift", [])}
    scores["drift"] = _prf(
        len(truth_drift & found_drift), len(found_drift), len(truth_drift)
    )

    # --- embedded formats
    truth_fmt = {(f["table"].lower(), f["column"].lower()) for f in manifest["embedded_formats"]}
    found_fmt = {(f.get("table", "").lower(), f.get("column", "").lower())
                 for f in model.get("embedded_formats", [])}
    scores["embedded_formats"] = _prf(
        len(truth_fmt & found_fmt), len(found_fmt), len(truth_fmt)
    )

    # --- god tables
    truth_god = {g["table"].lower() for g in manifest["god_tables"]}
    found_god = {g.get("table", "").lower() for g in model.get("god_tables", [])}
    scores["god_tables"] = _prf(len(truth_god & found_god), len(found_god), len(truth_god))

    return scores


# ------------------------------------------------------------------------- run scoring


@dataclass
class QuestionOutcome:
    id: str
    tags: list[str]
    difficulty: str
    correct: bool
    reason: str = ""
    abstained: bool = False
    cost_guard: bool = False
    clarified: bool = False
    tokens: int = 0
    latency_ms: int = 0
    verified_hit: bool = False
    confidence: float = 0.0
    sql: str = ""


def score_run(
    suite: Suite,
    clean_db: Path | None,
    outcomes: list[QuestionOutcome],
) -> dict[str, Any]:
    """Aggregate a run into the report the Lab and docs/PROGRESS.md consume."""
    by_id = {o.id: o for o in outcomes}
    executable = [q for q in suite.questions if q.is_executable]
    behavioural = [q for q in suite.questions if not q.is_executable]

    scored = [by_id[q.id] for q in executable if q.id in by_id]
    overall = sum(1 for o in scored if o.correct) / len(scored) if scored else 0.0

    per_tag: dict[str, dict[str, Any]] = {}
    for q in executable:
        o = by_id.get(q.id)
        if o is None:
            continue
        for tag in q.tags:
            bucket = per_tag.setdefault(tag, {"correct": 0, "total": 0})
            bucket["total"] += 1
            bucket["correct"] += int(o.correct)
    for bucket in per_tag.values():
        bucket["ex"] = round(bucket["correct"] / bucket["total"], 3) if bucket["total"] else 0.0

    per_difficulty: dict[str, dict[str, Any]] = {}
    for q in executable:
        o = by_id.get(q.id)
        if o is None:
            continue
        bucket = per_difficulty.setdefault(q.difficulty, {"correct": 0, "total": 0})
        bucket["total"] += 1
        bucket["correct"] += int(o.correct)
    for bucket in per_difficulty.values():
        bucket["ex"] = round(bucket["correct"] / bucket["total"], 3) if bucket["total"] else 0.0

    # Clarification: did we ask exactly when the question was annotated ambiguous?
    ambiguous = {q.id for q in suite.questions if q.ambiguity}
    asked = {o.id for o in outcomes if o.clarified}
    clar_tp = len(ambiguous & asked)
    clarification = _prf(clar_tp, len(asked), len(ambiguous))

    # Behavioural questions: cost guard and abstention.
    behaviour: dict[str, dict[str, Any]] = {}
    for expect in ("cost_guard", "abstain"):
        wanted = [q for q in behavioural if q.expect == expect]
        got = 0
        for q in wanted:
            o = by_id.get(q.id)
            if o is None:
                continue
            got += int(o.cost_guard if expect == "cost_guard" else o.abstained)
        behaviour[expect] = {
            "triggered": got,
            "total": len(wanted),
            "rate": round(got / len(wanted), 3) if wanted else 0.0,
        }
    # Abstaining on a question that had a real answer is a false abstention.
    false_abstentions = sum(1 for q in executable
                            if (o := by_id.get(q.id)) and o.abstained)
    behaviour["false_abstentions"] = {
        "count": false_abstentions,
        "of": len(executable),
    }

    tokens = [o.tokens for o in outcomes if o.tokens]
    latency = [o.latency_ms for o in outcomes if o.latency_ms]
    return {
        "suite": suite.name,
        "database": suite.database,
        "questions_scored": len(scored),
        "execution_accuracy": round(overall, 3),
        "per_tag": dict(sorted(per_tag.items())),
        "per_difficulty": per_difficulty,
        "clarification": clarification,
        "behaviour": behaviour,
        "verified_hit_rate": round(
            sum(1 for o in outcomes if o.verified_hit) / len(outcomes), 3
        ) if outcomes else 0.0,
        "mean_tokens": round(sum(tokens) / len(tokens)) if tokens else 0,
        "max_tokens": max(tokens) if tokens else 0,
        "mean_latency_ms": round(sum(latency) / len(latency)) if latency else 0,
        "failures": [
            {"id": o.id, "reason": o.reason, "tags": o.tags}
            for o in scored if not o.correct
        ],
    }


def render_report(report: dict[str, Any]) -> str:
    """The markdown table that gets pasted into docs/PROGRESS.md."""
    lines = [
        f"### {report['suite']} on {report['database']}",
        "",
        f"**EX {report['execution_accuracy']:.1%}** "
        f"over {report['questions_scored']} scored questions · "
        f"mean {report['mean_tokens']} tokens · {report['mean_latency_ms']} ms",
        "",
        "| anti-pattern | EX | n |",
        "|---|---:|---:|",
    ]
    for tag, bucket in report["per_tag"].items():
        lines.append(f"| {tag} | {bucket['ex']:.1%} | {bucket['total']} |")
    lines += [
        "",
        "| behaviour | rate |",
        "|---|---:|",
        f"| clarification precision | {report['clarification']['precision']:.1%} |",
        f"| clarification recall | {report['clarification']['recall']:.1%} |",
        f"| cost guard triggered | {report['behaviour']['cost_guard']['rate']:.1%} |",
        f"| correct abstention | {report['behaviour']['abstain']['rate']:.1%} |",
        f"| false abstentions | {report['behaviour']['false_abstentions']['count']} |",
        f"| verified hit rate | {report['verified_hit_rate']:.1%} |",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------- cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="chinook_v1")
    parser.add_argument("--clean", type=Path, default=None,
                        help="override a SQLite suite's clean copy; resolved from the suite otherwise")
    parser.add_argument("--check-gold", action="store_true",
                        help="verify both gold formulations agree")
    parser.add_argument("--knowledge", type=Path,
                        help="path to a semantic model JSON to score against the manifest")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="the answer key; defaults to the suite's legacy database")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    suite = Suite.load(HERE / "suites" / f"{args.suite}.yaml")
    if args.knowledge:
        from gauntlet.databases import manifest_path

        model = json.loads(args.knowledge.read_text("utf-8"))
        scores = knowledge_recall(args.manifest or manifest_path(suite.database), model)
        print(json.dumps(scores, indent=2))
        return 0

    if args.check_gold:
        return 1 if check_gold(suite, args.clean, args.verbose) else 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
