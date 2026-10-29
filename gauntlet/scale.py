"""A large legacy-shaped database, to measure how excavation scales.

Builds `n` tables with cryptic names, a surrogate key each, foreign-key-looking columns
that point at earlier tables (undeclared), a few value columns, and a share of backup and
scratch copies. The relationships are known by construction, so recall can be scored.

    python -m gauntlet.scale --tables 400 --rows 200

This measures wall time per stage, probes run and join recall. It is a speed and
scaling test on synthetic data, not an accuracy benchmark on a real schema.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
sys.path.insert(0, str(HERE.parent / "engine"))


def build(tables: int, rows: int, seed: int = 7) -> tuple[Path, set[tuple]]:
    rng = random.Random(seed)
    path = BUILD / f"scale_{tables}.sqlite"
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    truth: set[tuple] = set()
    names = [f"T{index:04d}_MSTR" for index in range(tables)]
    # Under about 20 distinct values the engine refuses to propose a join (a column holding
    # 1..8 is a subset of every key), so tables smaller than that cannot be recovered at all.
    sizes = {name: max(40, int(rows * rng.uniform(0.3, 1.5))) for name in names}
    for index, name in enumerate(names):
        key = f"T{index:04d}_ID"
        columns = [f"{key} INTEGER PRIMARY KEY"]
        parents = rng.sample(range(index), min(index, rng.randint(0, 3))) if index else []
        for parent in parents:
            columns.append(f"T{parent:04d}_ID INTEGER")
            truth.add((name.lower(), (f"t{parent:04d}_id",), names[parent].lower(),
                       (f"t{parent:04d}_id",)))
        columns += [f"AMT_{n} REAL" for n in range(3)] + ["NM VARCHAR(40)", "STS_CD CHAR(1)"]
        conn.execute(f"CREATE TABLE {name} ({', '.join(columns)})")
        data = []
        for row in range(1, sizes[name] + 1):
            values = [row]
            for parent in parents:
                values.append(rng.randint(1, sizes[names[parent]]))
            values += [round(rng.random() * 100, 2) for _ in range(3)]
            values += [f"name {row}", rng.choice("ACP")]
            data.append(values)
        marks = ",".join("?" * len(data[0]))
        conn.executemany(f"INSERT INTO {name} VALUES ({marks})", data)
        if index % 9 == 0:
            conn.execute(f"CREATE TABLE {name}_BAK_20230101 AS SELECT * FROM {name}")
        if index % 13 == 0:
            conn.execute(f"CREATE TABLE TMP_SCRATCH_{index} (ID INTEGER, V TEXT)")
    conn.commit()
    conn.close()
    return path, truth


def run(tables: int, rows: int) -> dict:
    from stratum.connectors.registry import build as connect
    from stratum.excavate.compile_model import compile_model
    from stratum.excavate.pipeline import excavate

    path, truth = build(tables, rows)
    connector = connect(f"sqlite:///{path}")
    connector.connect()
    started = time.perf_counter()
    result = excavate(connector)
    total = time.perf_counter() - started
    model = compile_model(result, database=f"scale_{tables}", profile_name=connector.profile.name)

    def key(child, ccols, parent, pcols):
        return (child.lower(), tuple(c.lower() for c in ccols), parent.lower(),
                tuple(p.lower() for p in pcols))

    found = {key(r.from_table, r.from_columns, r.to_table, r.to_columns)
             for r in model.relationships if getattr(r, "origin", "") != "conditional"
             and not r.condition}
    symmetric = found | {(p, pc, c, cc) for (c, cc, p, pc) in found}
    recovered = {t for t in truth if t in symmetric}
    stages = {t.stage: t.ms for t in result.trace}
    return {
        "tables": tables, "rows_per_table": rows,
        "columns": result.schema.total_columns(),
        "seconds": round(total, 1),
        "stage_ms": {k: v for k, v in stages.items() if v >= 200},
        "true_relationships": len(truth),
        "recovered": len(recovered),
        "join_recall": round(len(recovered) / len(truth), 3) if truth else 0.0,
        "relationships_reported": len(found),
        "join_precision": round(len({f for f in found if f in truth or (f[2], f[3], f[0], f[1]) in truth})
                                / len(found), 3) if found else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="gauntlet.scale")
    parser.add_argument("--tables", type=int, default=100)
    parser.add_argument("--rows", type=int, default=200)
    args = parser.parse_args()
    BUILD.mkdir(exist_ok=True)
    report = run(args.tables, args.rows)
    out = HERE / "reports" / f"scale_{args.tables}.json"
    out.write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
