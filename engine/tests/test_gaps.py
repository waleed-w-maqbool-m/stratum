"""Relationships and structure the database seems to be missing.

Built on `fixtures/build_gaps.py`. The rules these pin: gaps are suggestions that never
reach a query, every one carries its evidence, and the SQL written for them parses in the
dialect it claims.
"""

from __future__ import annotations

import pytest
import sqlglot

from stratum.api.review import apply_review_action
from stratum.connectors.registry import build
from stratum.excavate import gaps
from stratum.excavate.compile_model import compile_model
from stratum.excavate.ddl import foreign_key_ddl, lookup_ddl
from stratum.excavate.findings import Kind
from stratum.excavate.joins import Candidate, Rejected
from stratum.excavate.pipeline import excavate

from .fixtures.build_gaps import build as build_gaps


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    path = build_gaps(tmp_path_factory.mktemp("gaps") / "gaps.sqlite")
    conn = build(f"sqlite:///{path}")
    conn.connect()
    try:
        yield excavate(conn)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def model(result):
    return compile_model(result)


def _gap(model, gap_type, table):
    found = [g for g in model.gaps if g.gap_type == gap_type and g.table.lower() == table]
    assert found, f"no {gap_type} gap on {table}; have {[(g.gap_type, g.table) for g in model.gaps]}"
    return found[0]


def test_a_confident_relationship_with_no_constraint_is_reported(model):
    gap = _gap(model, "undeclared_fk", "line")
    assert gap.to_table == "orders" and gap.columns == ["order_id"]
    assert "SQLite cannot add a foreign key" in gap.fix
    assert gap.blockers == []
    assert gap.evidence["inclusion"] == 1.0


def test_a_relationship_eroded_by_deleted_parents_is_a_broken_relationship(model):
    gap = _gap(model, "broken_relationship", "orders")
    assert gap.to_table == "customer"
    assert 0.5 <= gap.evidence["inclusion"] < 0.85
    assert gap.evidence["orphan_count"] == 20
    assert gap.evidence["orphan_samples"]
    assert "deleted parent rows" in gap.reason


def test_a_reference_that_mostly_matches_nothing_is_dangling(model):
    gap = _gap(model, "dangling_reference", "ship")
    assert gap.to_table == "carrier"
    assert 0.15 <= gap.evidence["inclusion"] < 0.5
    assert gap.confidence <= 0.5


def test_a_dangling_reference_that_matches_a_backup_points_outside_core(model):
    gap = _gap(model, "points_outside_core", "invoice")
    assert gap.to_table == "vendor_bak_20240101"
    assert gap.evidence["live_parent"] == "vendor"
    assert gap.evidence["inclusion"] == 1.0
    # It replaced the dangling report rather than sitting beside it.
    assert not [g for g in model.gaps if g.gap_type == "dangling_reference"
                and g.table == "invoice"]


def test_a_coded_column_with_no_lookup_table_gets_one_proposed(model):
    gap = _gap(model, "missing_lookup", "ticket")
    assert gap.columns == ["stat_cd"]
    assert gap.evidence["codes"] == 3
    assert "Open" in gap.reason or "O" in gap.reason


def test_a_table_nothing_relates_to_is_isolated(model):
    gap = _gap(model, "isolated_table", "audit_blob")
    assert gap.confidence <= 0.3


def test_gaps_are_suggestions_and_never_become_relationships_unasked(model):
    joined = {(r.from_table.lower(), r.to_table.lower()) for r in model.relationships}
    assert ("orders", "customer") not in joined          # broken: below the inclusion bar
    assert ("ship", "carrier") not in joined
    assert ("invoice", "vendor_bak_20240101") not in joined
    assert all(g.status == "hypothesis" for g in model.gaps)
    assert all(g.confidence < 0.95 for g in model.gaps)


def test_accepting_a_broken_relationship_adds_it_with_its_provenance(result):
    model = compile_model(result)
    findings = [f.to_dict() for f in result.findings]
    gap = _gap(model, "broken_relationship", "orders")
    assert apply_review_action(model, findings, gap.id, "accept")
    added = [r for r in model.relationships if r.from_table.lower() == "orders"]
    assert len(added) == 1
    assert added[0].to_table.lower() == "customer"
    assert added[0].status == "accepted" and added[0].origin == "data"
    assert added[0].logical_on.endswith("customer.id") or "=" in added[0].logical_on
    assert added[0].provenance.reason.startswith("Accepted by a reviewer.")
    assert _gap(model, "broken_relationship", "orders").status == "accepted"


def test_rejecting_a_gap_changes_nothing_else(result):
    model = compile_model(result)
    findings = [f.to_dict() for f in result.findings]
    before = len(model.relationships)
    gap = _gap(model, "dangling_reference", "ship")
    assert apply_review_action(model, findings, gap.id, "reject")
    assert len(model.relationships) == before
    assert _gap(model, "dangling_reference", "ship").status == "rejected"


def test_gap_findings_carry_a_reason_and_are_hypotheses(result):
    for finding in result.findings.of(Kind.GAP):
        assert finding.evidence["reason"]
        assert finding.status.value == "hypothesis"


def test_the_gaps_stage_is_traced(result):
    stage = next(t for t in result.trace if t.stage == "gaps")
    assert stage.detail["gaps"] == len(result.findings.of(Kind.GAP))
    assert stage.detail["undeclared_fk"] >= 1


# ---------------------------------------------------------------- handcrafted cases


def _candidate(schema, child, child_cols, parent, parent_cols, **kw):
    return Candidate(
        child_table=schema.get(child), child_columns=child_cols,
        parent_table=schema.get(parent), parent_columns=parent_cols, **kw,
    )


def test_a_parent_that_is_not_unique_is_reported_once_not_in_both_directions(result):
    schema = result.schema
    forward = Rejected(_candidate(
        schema, "orders", ["cust_id"], "customer", ["cust_id"], name_score=1.0,
        inclusion=1.0, tested=80, parent_unique=False), "parent_not_unique")
    out = gaps.detect(None, schema, result.profiles, [], [forward], {}, [])
    unique = [g for g in out if g.value["gap_type"] == "missing_unique"]
    assert [g.target for g in unique] == ["orders"]

    backward = Rejected(_candidate(
        schema, "customer", ["cust_id"], "orders", ["cust_id"], name_score=1.0,
        inclusion=1.0, tested=60, parent_unique=False), "parent_not_unique")
    out = gaps.detect(None, schema, result.profiles, [], [forward, backward], {}, [])
    assert not [g for g in out if g.value["gap_type"] == "missing_unique"]


def test_too_few_values_are_not_evidence(result):
    schema = result.schema
    thin = Rejected(_candidate(
        schema, "orders", ["cust_id"], "customer", ["cust_id"], name_score=1.0,
        inclusion=0.7, tested=3, orphan_count=1), "inclusion")
    out = gaps.detect(None, schema, result.profiles, [], [thin], {}, [])
    assert not [g for g in out if g.value["gap_type"] == "broken_relationship"]


def test_a_tables_own_key_failing_to_match_another_table_is_not_a_gap(result):
    schema = result.schema
    own = Rejected(_candidate(
        schema, "customer", ["cust_id"], "orders", ["cust_id"], name_score=1.0,
        inclusion=0.4, tested=60), "inclusion")
    out = gaps.detect(None, schema, result.profiles, [], [own], {}, [])
    assert not [g for g in out if g.target == "customer"
                and g.value["gap_type"] != "isolated_table"]


# --------------------------------------------------------------------------- DDL


@pytest.mark.parametrize("dialect, read", [("mssql", "tsql"), ("postgres", "postgres")])
def test_the_foreign_key_statement_parses_in_its_dialect(dialect, read):
    for orphans in (0, 3):
        sql = foreign_key_ddl(
            dialect, "dbo.Orders", ["CustID"], "dbo.Customer", ["CustID"],
            orphans=orphans, parent_has_key=False,
        )
        statements = sqlglot.parse(sql, read=read)
        assert len([s for s in statements if s is not None]) == 2   # unique key + foreign key
    assert "WITH NOCHECK" in foreign_key_ddl(
        "mssql", "Orders", ["c"], "Customer", ["c"], orphans=2)
    assert "WITH CHECK" in foreign_key_ddl("mssql", "Orders", ["c"], "Customer", ["c"])
    assert "NOT VALID" in foreign_key_ddl(
        "postgres", "orders", ["c"], "customer", ["c"], orphans=2)


def test_the_lookup_statements_parse_and_escape_quotes():
    sql = lookup_ddl("postgres", "public.ticket", "stat_cd", "TEXT",
                     {"O": "Open", "X": "Won't fix"})
    assert "'Won''t fix'" in sql
    assert len([s for s in sqlglot.parse(sql, read="postgres") if s is not None]) == 3
    assert "CREATE TABLE" in lookup_ddl("mssql", "dbo.Ticket", "Stat", "CHAR(1)", {"O": "Open"})
    assert lookup_ddl("sqlite", "ticket", "stat_cd", "TEXT", {"O": "Open"}).startswith("--")


# ---------------------------------------------------------------------------- API


def test_the_api_serves_gaps_and_draws_the_ones_that_name_a_table(result, tmp_path, monkeypatch):
    from stratum.api import app as api
    from stratum.semantic.store import ModelStore

    store = ModelStore(tmp_path)
    model = compile_model(result, database="gapdb")
    store.save(model)
    monkeypatch.setattr(api.registry, "store", store)

    listing = api.gaps("gapdb")
    assert listing["total"] == len(model.gaps)
    assert listing["by_type"]["undeclared_fk"] >= 1
    assert [g["confidence"] for g in listing["items"]] == sorted(
        (g["confidence"] for g in listing["items"]), reverse=True
    )
    assert all(g["gap_type"] == "isolated_table" for g in api.gaps("gapdb", "isolated_table")["items"])

    payload = api.atlas("gapdb")
    ghosts = {(e["from"], e["to"], e["gap_type"]) for e in payload["ghost_edges"]}
    assert ("orders", "customer", "broken_relationship") in ghosts
    assert ("invoice", "vendor_bak_20240101", "points_outside_core") in ghosts
    # A confident undeclared key is a real edge already, so it is not drawn twice.
    assert not [e for e in payload["ghost_edges"] if e["gap_type"] == "undeclared_fk"]
    assert payload["summary"]["gaps_by_type"]["broken_relationship"] == 1


def test_a_join_whose_parent_is_unique_only_under_a_filter_is_not_offered_as_a_foreign_key(result):
    from stratum.excavate.findings import Finding, Status

    schema = result.schema
    # Orders are unique only where a discriminator holds, as in a god table.
    join = Finding(
        kind=Kind.JOIN, target="line",
        value={"from_table": "line", "from_columns": ["order_id"],
               "to_table": "orders", "to_columns": ["cust_id"], "expression": ""},
        confidence=0.9, status=Status.HYPOTHESIS, detector="joins",
        evidence={"origin": "data", "inclusion": 1.0, "distinct_values_tested": 80,
                  "parent_filter": "kind = 'I'", "parent_key_is_unique": True},
    )
    out = gaps.detect(None, schema, result.profiles, [join], [], {}, [])
    found = [g for g in out if g.target == "line"]
    assert [g.value["gap_type"] for g in found] == ["missing_unique"]
    assert found[0].evidence["fix"] == ""
    assert "kind = 'I'" in found[0].evidence["reason"]


def test_the_atlas_has_an_as_submitted_view_and_a_table_inspector(result, tmp_path, monkeypatch):
    from stratum.api import app as api
    from stratum.semantic.store import ModelStore

    store = ModelStore(tmp_path)
    store.save(compile_model(result, database="gapdb"))
    store.save_findings("gapdb", [f.to_dict() for f in result.findings])
    store.save_catalog("gapdb", result.schema.to_catalog())
    monkeypatch.setattr(api.registry, "store", store)

    submitted = api.atlas("gapdb", view="submitted")
    refined = api.atlas("gapdb")
    # Nothing is judged in the submitted view: every table is live, and none is renamed.
    assert submitted["summary"]["excluded"] == 0
    assert {n["class"] for n in submitted["nodes"]} == {"core"}
    assert all(n["entity"] is None and n["position"][1] == 0.0 for n in submitted["nodes"])
    assert submitted["edges"] == []                       # this database declares no keys
    assert refined["summary"]["excluded"] >= 1            # the backup table
    assert refined["summary"]["entities"] >= 1

    detail = api.table_detail("gapdb", "orders")
    columns = {c["physical"]: c for c in detail["columns"]}
    assert columns["cust_id"]["logical"] and columns["cust_id"]["type"]
    assert detail["entity"] and detail["kind"] == "core"
    assert any(g["gap_type"] == "broken_relationship" for g in detail["gaps"])
    assert api.table_detail("gapdb", "ticket")["table"] == "ticket"

    raw = api.table_detail("gapdb", "orders", view="submitted")
    assert raw["entity"] is None and raw["columns"][0]["logical"] == ""

    store_without_catalog = ModelStore(tmp_path / "empty")
    store_without_catalog.save(compile_model(result, database="old"))
    monkeypatch.setattr(api.registry, "store", store_without_catalog)
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException) as excinfo:
        api.atlas("old", view="submitted")
    assert excinfo.value.status_code == 404
    assert api.table_detail("old", "orders")["entity"]     # refined still works without it


# ------------------------------------------------------------------ the compiler


def _logical(model, table, physical):
    entity = model.entity_for_table(table)
    column = next(c for c in entity.columns.values() if c.physical_column == physical)
    return entity.name, column.name


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    path = build_gaps(tmp_path_factory.mktemp("compile") / "gaps.sqlite")
    conn = build(f"sqlite:///{path}")
    conn.connect()
    try:
        yield conn, compile_model(excavate(conn))
    finally:
        conn.close()


@pytest.mark.parametrize("push", [True, False])
def test_an_anti_join_filter_on_the_nullable_side_is_not_pushed_into_the_join(live, push):
    """c-021: `WHERE o.id IS NULL` pushed into `o` made every row look unmatched."""
    from stratum.ask.compiler import compile_logical
    from stratum.connectors.executor import ReadOnlyExecutor

    conn, model = live
    customer, cust_key = _logical(model, "customer", "cust_id")
    orders, order_cust = _logical(model, "orders", "cust_id")
    _, order_key = _logical(model, "orders", "order_id")
    sql = (
        f"SELECT COUNT(*) AS n FROM {customer} c LEFT JOIN {orders} o "
        f"ON o.{order_cust} = c.{cust_key} AND o.{order_key} < 3 "
        f"WHERE o.{order_key} IS NULL"
    )
    compiled = compile_logical(sql, model, conn.profile, push_predicates=push)
    # Orders 1 and 2 belong to customers 2 and 3, so 58 of 60 customers have no such order.
    assert ReadOnlyExecutor(conn, conn.profile).run(compiled.physical_sql).rows[0][0] == 58


def test_a_filter_on_the_preserved_side_of_a_left_join_is_still_pushed(live):
    from stratum.ask.compiler import compile_logical

    conn, model = live
    customer, cust_key = _logical(model, "customer", "cust_id")
    orders, order_cust = _logical(model, "orders", "cust_id")
    sql = (
        f"SELECT COUNT(*) AS n FROM {customer} c LEFT JOIN {orders} o "
        f"ON o.{order_cust} = c.{cust_key} WHERE c.{cust_key} < 10"
    )
    physical = compile_logical(sql, model, conn.profile).physical_sql
    assert physical.lower().count("< 10") == 2      # once inside c's table, once outside
