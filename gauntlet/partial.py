"""A legacy database that kept some of its foreign keys: `chinook_partial`.

The benchmark databases drop every foreign key, so they cannot test what Stratum does with
constraints the schema *does* declare, or whether it tells a declared key from a guessed
one. This builds a copy of `chinook_legacy` in which half of the real, unconditional joins
are declared constraints and the other half are still undeclared, and scores two things:

* every declared key is labelled declared
* every dropped key comes back as an `undeclared_fk` suggestion, and nothing else does

Like the legacifier this is benchmark tooling: it reads the manifest to choose which keys
to declare, and the scorer reads it to mark the answers. The engine never does.

    python -m gauntlet.partial build
    python -m gauntlet.partial score
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
REPORTS = HERE / "reports"

SOURCE = "chinook_legacy"
TARGET = "chinook_partial"


def _join_key(child: str, ccols: list[str], parent: str, pcols: list[str]) -> tuple:
    return (child.lower(), tuple(sorted(c.lower() for c in ccols)),
            parent.lower(), tuple(sorted(c.lower() for c in pcols)))


def unconditional_joins(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The joins that could be foreign keys: those that hold for every row."""
    return sorted(
        (j for j in manifest["joins"] if not j.get("condition")),
        key=lambda j: (j["child"].lower(), j["child_cols"], j["parent"].lower()),
    )


def choose_kept(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every second unconditional join, by a fixed order, so the split is reproducible."""
    return unconditional_joins(manifest)[::2]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def add_foreign_keys(conn: sqlite3.Connection, table: str, keys: list[dict[str, Any]]) -> None:
    """Rebuild `table` with the given foreign keys, keeping its data, indexes and views.

    SQLite has no `ALTER TABLE ADD CONSTRAINT`, so this is the documented rebuild: create
    the new table, copy the rows, drop the old one, rename. `legacy_alter_table` stops the
    rename from refusing because a view still names the table.
    """
    columns = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    definitions = []
    for _cid, name, type_, notnull, default, _pk in columns:
        text = f"{_quote(name)} {type_ or ''}".strip()
        if notnull:
            text += " NOT NULL"
        if default is not None:
            text += f" DEFAULT {default}"
        definitions.append(text)
    primary = [c[1] for c in sorted((c for c in columns if c[5]), key=lambda c: c[5])]
    if primary:
        definitions.append(f"PRIMARY KEY ({', '.join(_quote(c) for c in primary)})")
    for key in keys:
        definitions.append(
            f"FOREIGN KEY ({', '.join(_quote(c) for c in key['child_cols'])}) "
            f"REFERENCES {_quote(key['parent'])} "
            f"({', '.join(_quote(c) for c in key['parent_cols'])})"
        )

    indexes = [
        row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (table,)
        )
    ]
    temp = f"{table}__rebuild"
    conn.execute(f"CREATE TABLE {_quote(temp)} ({', '.join(definitions)})")
    conn.execute(f"INSERT INTO {_quote(temp)} SELECT * FROM {_quote(table)}")
    conn.execute(f"DROP TABLE {_quote(table)}")
    conn.execute(f"ALTER TABLE {_quote(temp)} RENAME TO {_quote(table)}")
    for statement in indexes:
        conn.execute(statement)


def build() -> Path:
    source, target = BUILD / f"{SOURCE}.sqlite", BUILD / f"{TARGET}.sqlite"
    manifest = json.loads((BUILD / f"{SOURCE}.manifest.json").read_text("utf-8"))
    kept = choose_kept(manifest)

    # Work on a copy. The benchmark's own legacy database is never modified.
    original = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        rows_before = {
            name: original.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
            for (name,) in original.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        original.close()
    target.unlink(missing_ok=True)
    shutil.copyfile(source, target)

    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("PRAGMA foreign_keys=OFF")
        by_table: dict[str, list[dict[str, Any]]] = {}
        for key in kept:
            by_table.setdefault(key["child"], []).append(key)
        for table, keys in by_table.items():
            add_foreign_keys(conn, table, keys)
        conn.commit()
        for name, count in rows_before.items():
            after = conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
            assert after == count, f"{name}: {count} rows became {after}"
        declared = sum(
            len(conn.execute(f"PRAGMA foreign_key_list({_quote(t)})").fetchall()) > 0
            for t in by_table
        )
        assert declared == len(by_table), "a rebuilt table lost its foreign keys"
    finally:
        conn.close()
    return target


def score() -> dict[str, Any]:
    sys.path.insert(0, str(HERE.parent / "engine"))
    from gauntlet.scorer import declared_recall, knowledge_recall
    from stratum.connectors.registry import build as connect
    from stratum.excavate.compile_model import compile_model
    from stratum.excavate.pipeline import excavate

    manifest_file = BUILD / f"{SOURCE}.manifest.json"
    manifest = json.loads(manifest_file.read_text("utf-8"))
    kept = choose_kept(manifest)

    connector = connect(f"sqlite:///{BUILD / f'{TARGET}.sqlite'}")
    connector.connect()
    result = excavate(connector)
    model = compile_model(result, database=TARGET, profile_name=connector.profile.name)
    dump = model.model_dump()

    scores = {
        "database": TARGET,
        "declared_keys": len(kept),
        "undeclared_keys": len(unconditional_joins(manifest)) - len(kept),
        **declared_recall(manifest_file, [
            _join_key(k["child"], k["child_cols"], k["parent"], k["parent_cols"]) for k in kept
        ], dump),
        "knowledge": knowledge_recall(manifest_file, dump),
    }
    return scores


def main() -> int:
    parser = argparse.ArgumentParser(prog="gauntlet.partial")
    parser.add_argument("command", choices=["build", "score"])
    args = parser.parse_args()
    if args.command == "build":
        print(build())
        return 0
    scores = score()
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{TARGET}.recall.json"
    out.write_text(json.dumps(scores, indent=2), "utf-8")
    print(json.dumps({k: v for k, v in scores.items() if k != "knowledge"}, indent=2))
    print("joins (all):", scores["knowledge"]["joins"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
