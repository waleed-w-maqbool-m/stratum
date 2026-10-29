"""`chinook_partial` is built by rebuilding tables, so the build is what needs proving.

Rebuilding a table to add a foreign key can silently lose rows, an index, or the view that
names it. And the scorer for it has to tell a declared key from a guessed one without being
fooled by direction or schema prefixes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gauntlet import partial  # noqa: E402
from gauntlet.scorer import declared_recall  # noqa: E402


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT 'x');
        CREATE TABLE child (cid INTEGER, pid INTEGER, note TEXT, PRIMARY KEY (cid));
        CREATE INDEX child_pid ON child (pid);
        CREATE VIEW v_child AS SELECT c.cid, p.name FROM child c JOIN parent p ON c.pid = p.id;
        INSERT INTO parent VALUES (1, 'a'), (2, 'b');
        INSERT INTO child VALUES (10, 1, 'n1'), (11, 2, NULL), (12, 99, 'orphan');
        """
    )
    return conn


def test_adding_a_foreign_key_keeps_rows_indexes_views_and_orphans():
    conn = _database()
    conn.execute("PRAGMA legacy_alter_table=ON")
    partial.add_foreign_keys(conn, "child", [
        {"child": "child", "child_cols": ["pid"], "parent": "parent", "parent_cols": ["id"]},
    ])
    assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 3
    assert conn.execute("SELECT note FROM child WHERE cid = 12").fetchone()[0] == "orphan"
    [fk] = conn.execute("PRAGMA foreign_key_list(child)").fetchall()
    assert (fk[2], fk[3], fk[4]) == ("parent", "pid", "id")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'child_pid'").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM v_child").fetchone()[0] == 2
    assert [c[1] for c in conn.execute("PRAGMA table_info(parent)")] == ["id", "name"]


def test_the_split_is_every_second_unconditional_join_and_is_reproducible():
    manifest = {"joins": [
        {"child": "B", "child_cols": ["x"], "parent": "P", "parent_cols": ["id"], "condition": ""},
        {"child": "A", "child_cols": ["x"], "parent": "P", "parent_cols": ["id"], "condition": ""},
        {"child": "C", "child_cols": ["x"], "parent": "P", "parent_cols": ["id"],
         "condition": "kind = 'I'"},
        {"child": "D", "child_cols": ["x"], "parent": "P", "parent_cols": ["id"], "condition": ""},
    ]}
    kept = partial.choose_kept(manifest)
    assert [k["child"] for k in kept] == ["A", "D"]        # sorted, every second, no conditional
    assert kept == partial.choose_kept(manifest)


def test_declared_recall_separates_declared_from_guessed(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"joins": [
        {"child": "ORD", "child_cols": ["CUST"], "parent": "CUST", "parent_cols": ["ID"],
         "condition": ""},
        {"child": "LINE", "child_cols": ["ORD"], "parent": "ORD", "parent_cols": ["ID"],
         "condition": ""},
        {"child": "REF", "child_cols": ["X"], "parent": "CUST", "parent_cols": ["ID"],
         "condition": "kind = 'c'"},
    ]}))
    kept = [("ord", ("cust",), "cust", ("id",))]
    model = {
        "relationships": [
            # Declared, and found backwards: direction must not matter.
            {"from_table": "dbo.CUST", "from_columns": ["ID"], "to_table": "dbo.ORD",
             "to_columns": ["CUST"], "origin": "declared"},
            {"from_table": "LINE", "from_columns": ["ORD"], "to_table": "ORD",
             "to_columns": ["ID"], "origin": "data"},
        ],
        "gaps": [
            {"gap_type": "undeclared_fk", "table": "LINE", "columns": ["ORD"],
             "to_table": "ORD", "to_columns": ["ID"]},
            {"gap_type": "undeclared_fk", "table": "ORD", "columns": ["CUST"],
             "to_table": "CUST", "to_columns": ["ID"]},     # wrong: this one was declared
            {"gap_type": "isolated_table", "table": "X", "columns": [], "to_table": "",
             "to_columns": []},
        ],
    }
    scores = declared_recall(manifest, kept, model)
    assert scores["declared_labelled_recall"] == 1.0
    assert scores["declared_labelled_precision"] == 1.0
    assert scores["undeclared_gap_recall"] == 1.0
    assert scores["undeclared_gap_precision"] == 0.5
    assert scores["kept_reported_as_undeclared"] == 1


# ------------------------------------------------------------------ tie-aware scoring


def test_tie_aware_accepts_ties_listed_in_a_different_order_but_nothing_else():
    from gauntlet.scorer import compare, compare_tie_aware

    gold = [("Ann", 90), ("Bob", 80), ("Cy", 80), ("Di", 70)]
    swapped = [("Ann", 90), ("Cy", 80), ("Bob", 80), ("Di", 70)]       # tied totals reordered
    assert not compare(gold, swapped, order_sensitive=True).correct
    assert compare_tie_aware(gold, swapped).correct

    wrong_order = [("Di", 70), ("Bob", 80), ("Cy", 80), ("Ann", 90)]   # sorted the other way
    assert not compare_tie_aware(gold, wrong_order).correct
    different_rows = [("Ann", 90), ("Bob", 80), ("Eve", 80), ("Di", 70)]
    assert not compare_tie_aware(gold, different_rows).correct
    assert compare_tie_aware(gold, gold).correct


def test_tie_aware_does_not_rescue_a_missing_gold_column():
    from gauntlet.scorer import compare_tie_aware

    gold = [("Ann", 90, 3), ("Bob", 80, 2)]
    assert not compare_tie_aware(gold, [("Ann", 90), ("Bob", 80)]).correct
