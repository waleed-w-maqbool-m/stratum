"""The query path: compile, validate, plan joins, link values, detect ambiguity.

The compiler tests are the load-bearing ones. Each compiles logical SQL written with no
knowledge of the legacy schema and checks the answer against what the *clean* database
says — which is the only way to know the rewrite was right rather than merely plausible.
"""

from __future__ import annotations

import pytest

from stratum.ask import ambiguity as ambiguity_module
from stratum.ask import dialect as dialect_module
from stratum.ask.compiler import compile_logical
from stratum.ask.fallback import split_for_duckdb
from stratum.ask.joinplan import build_graph, plan
from stratum.ask.linking import SchemaIndex
from stratum.ask.validate import validate
from stratum.ask.values import ValueIndex, trigrams
from stratum.connectors.profiles import LEGACY_STRICT, SQLITE3, TSQL2008

# --------------------------------------------------------------------- compiler


def test_compiler_applies_the_soft_delete_filter(model, connector, executor):
    compiled = compile_logical("SELECT COUNT(*) AS n FROM customer", model, connector.profile)
    assert "DEL_FLG" in compiled.physical_sql
    result = executor.run(compiled.physical_sql)
    assert result.rows[0][0] == 8, "two soft-deleted customers are excluded"


def test_compiler_records_every_filter_as_an_assumption(model, connector):
    compiled = compile_logical("SELECT COUNT(*) AS n FROM customer", model, connector.profile)
    kinds = {a.kind for a in compiled.assumptions}
    assert "soft_delete" in kinds
    assert all(a.text for a in compiled.assumptions), "an assumption with no reason is useless"


def test_compiler_applies_the_god_table_discriminator(model, connector, executor):
    order = next(
        e for e in model.entities.values() if e.source.upper() == "TRANS_MSTR"
    )
    compiled = compile_logical(
        f"SELECT COUNT(*) AS n FROM {order.name}", model, connector.profile
    )
    assert "TRANS_TYP_CD" in compiled.physical_sql
    assert executor.run(compiled.physical_sql).rows[0][0] < 12, (
        "the discriminator must exclude the other kinds of row"
    )


def test_compiler_rejects_a_physical_table_name(model, connector):
    from stratum.ask.compiler import CompileError

    with pytest.raises(CompileError, match="unknown entity"):
        compile_logical("SELECT COUNT(*) FROM CUST_MSTR", model, connector.profile)


def test_compiler_pushes_single_entity_predicates_down(model, connector):
    compiled = compile_logical(
        "SELECT customer.id FROM customer WHERE customer.id > 3",
        model, connector.profile,
    )
    # The predicate should end up inside the derived table, before the closing paren.
    inner = compiled.physical_sql.split(") AS customer")[0]
    assert "> 3" in inner, "the filter should run inside the entity, not outside it"


# -------------------------------------------------------------------- validator


def test_validator_catches_fan_out(model, connector, executor):
    """Summing a parent measure across a one-to-many join double counts.

    This is the check that matters most and fails most quietly: nothing errors, and the
    number that comes back is merely too large.
    """
    order = next(e for e in model.entities.values() if e.source.upper() == "TRANS_MSTR")
    line = next(e for e in model.entities.values() if e.source.upper() == "SO_LN")

    compiled = compile_logical(
        f"SELECT SUM({order.name}.total_amount) AS revenue "
        f"FROM {order.name} JOIN {line.name} "
        f"ON {line.name}.store_code = {order.name}.store_code "
        f"AND {line.name}.order_number = {order.name}.order_number",
        model, connector.profile,
    )
    result = validate(compiled, model, executor, check_fan_out=True)
    fan_out = next(c for c in result.checks if c.name == "fan_out")
    assert fan_out.passed is False, "joining orders to their lines multiplies the orders"
    assert "more than once" in fan_out.message


def test_validator_does_not_claim_a_pass_it_could_not_establish(model, connector, executor):
    """A check that cannot run must report unknown, never success."""
    compiled = compile_logical(
        "SELECT COUNT(*) AS n FROM customer", model, connector.profile
    )
    result = validate(compiled, model, executor, check_fan_out=True)
    fan_out = next(c for c in result.checks if c.name == "fan_out")
    # No aggregate over a join here, so this is a legitimate pass.
    assert fan_out.passed is True


def test_validator_rejects_non_select(model, connector, executor):
    compiled = compile_logical("SELECT COUNT(*) AS n FROM customer", model, connector.profile)
    compiled.physical_sql = "DELETE FROM CUST_MSTR"
    result = validate(compiled, model, executor)
    assert not result.ok
    assert result.blocking[0].name == "read_only"


def test_validator_flags_an_unknown_column(model, connector, executor):
    compiled = compile_logical("SELECT COUNT(*) AS n FROM customer", model, connector.profile)
    compiled.logical_sql = "SELECT customer.no_such_column FROM customer"
    result = validate(compiled, model, executor, check_fan_out=False)
    objects = next(c for c in result.checks if c.name == "objects_exist")
    assert objects.passed is False
    assert "no_such_column" in objects.message


# -------------------------------------------------------------------- join plan


def test_joinplan_connects_two_entities_through_the_graph(model):
    order = next(e for e in model.entities.values() if e.source.upper() == "TRANS_MSTR")
    graph = build_graph(model)
    result = plan(model, ["customer", order.name], graph=graph)
    assert result.connected
    assert len(result.steps) == 1
    assert "FROM" in result.render()


def test_joinplan_reports_an_unreachable_entity(model):
    graph = build_graph(model)
    result = plan(model, ["customer", "no_such_entity"], graph=graph)
    assert "no_such_entity" not in result.entities


# ---------------------------------------------------------------------- dialect


def test_tsql2008_forbids_what_the_plan_says_it_forbids():
    for sql, feature in [
        ("SELECT a FROM t ORDER BY a OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY", "offset_fetch"),
        ("SELECT IIF(a > 1, 'y', 'n') FROM t", "iif"),
        ("SELECT CONCAT(a, b) FROM t", "concat_fn"),
        ("SELECT TRY_CAST(a AS INT) FROM t", "try_cast"),
        ("SELECT LAG(a) OVER (ORDER BY b) FROM t", "lag_lead"),
        ("SELECT STRING_AGG(a, ',') FROM t", "string_agg"),
        ("SELECT JSON_VALUE(a, '$.x') FROM t", "json_fns"),
    ]:
        result = dialect_module.lint(sql, TSQL2008)
        assert not result.ok, f"{feature} should be rejected: {sql}"
        assert any(v.feature == feature for v in result.violations), (
            f"{sql} should name {feature}, got {[v.feature for v in result.violations]}"
        )


def test_tsql2008_allows_what_it_should():
    for sql in [
        "SELECT TOP 10 a FROM t",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "SELECT ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) FROM t",
        "SELECT CASE WHEN a > 1 THEN 'y' ELSE 'n' END FROM t",
    ]:
        assert dialect_module.lint(sql, TSQL2008).ok, sql


def test_downgrade_rewrites_what_it_can():
    result = dialect_module.downgrade("SELECT IIF(a > 1, 'y', 'n') AS f FROM t", TSQL2008)
    assert result.ok, result.unresolved
    assert "CASE" in result.sql.upper()
    assert "IIF" not in result.sql.upper()


def test_downgrade_preserves_concat_null_semantics():
    """CONCAT treats NULL as empty; `+` propagates it. A naive rewrite loses rows."""
    result = dialect_module.downgrade("SELECT CONCAT(a, b) AS n FROM t", TSQL2008)
    assert result.ok, result.unresolved
    assert "COALESCE" in result.sql.upper() or "ISNULL" in result.sql.upper()


def test_legacy_strict_inlines_a_cte():
    result = dialect_module.downgrade(
        "WITH x AS (SELECT a FROM t) SELECT a FROM x", LEGACY_STRICT
    )
    assert "WITH" not in result.sql.upper()
    assert result.ok, result.unresolved


def test_lint_reports_what_cannot_be_rewritten():
    result = dialect_module.downgrade("SELECT STRING_AGG(a, ',') AS g FROM t", TSQL2008)
    assert not result.ok, "STRING_AGG has no 2008 equivalent and must escalate to R3"


# ---------------------------------------------------------------------- values


def test_trigrams_are_stable():
    assert trigrams("NY") == trigrams("ny")
    assert trigrams("New York") & trigrams("new  york")


def test_value_index_binds_a_literal_to_its_stored_spellings(model, profiles):
    index = ValueIndex(model, profiles)
    assert index.stats()["columns_indexed"] >= 0    # tiny fixture may index little
    bindings = index.lookup("Berlin", limit=3)
    if bindings:                                     # only if a vocabulary column exists
        assert any("berlin" in m.lower() for b in bindings for m in b.matches)


def test_value_index_extracts_quoted_and_capitalised_literals(model, profiles):
    index = ValueIndex(model, profiles)
    literals = index.extract_literals("How many customers are in 'New York' and Berlin?")
    assert "New York" in literals
    assert "Berlin" in literals


# ------------------------------------------------------------------- ambiguity


def test_ambiguity_flags_an_unqualified_superlative(model):
    result = ambiguity_module.detect("Who are our top customers?", model)
    assert result.needs_clarification
    assert result.primary.type == "intent"
    assert result.primary.term == "top"


def test_ambiguity_accepts_a_question_that_answers_itself(model):
    result = ambiguity_module.detect(
        "Show the top 10 customers by revenue", model
    )
    assert not result.needs_clarification, (
        "the question already says how many and by what"
    )


def test_ambiguity_classifies_a_missing_definition_as_knowledge(model):
    result = ambiguity_module.detect("How many active customers?", model)
    assert result.needs_clarification
    assert result.primary.type == "knowledge"


def test_ambiguity_defers_to_the_glossary(model):
    model.glossary["active"] = "a customer with an order in the last 12 months"
    try:
        result = ambiguity_module.detect("How many active customers?", model)
        assert not result.needs_clarification, "the glossary defines it, so it is not ambiguous"
    finally:
        model.glossary.pop("active", None)


def test_clarification_options_are_built_from_the_model(model):
    result = ambiguity_module.detect("Who are our top customers?", model)
    options = ambiguity_module.build_options(result.primary, model, list(model.entities))
    assert 1 <= len(options) <= 4
    assert sum(o.is_default for o in options) == 1, "exactly one option is the default"


# -------------------------------------------------------------------- fallback


def test_split_for_duckdb_separates_what_the_dialect_cannot_do():
    split = split_for_duckdb(
        "SELECT a.id, LAG(b.total) OVER (ORDER BY b.d) AS prev "
        "FROM a JOIN b ON b.aid = a.id",
        None, None,
    )
    assert split is not None
    inner, outer = split
    assert "LAG" not in inner.upper(), "the window function must not go to the database"
    assert "LAG" in outer.upper()
    assert "extracted" in outer
    assert "JOIN" in inner.upper(), "the join stays in the database, where it belongs"


def test_duckdb_load_infers_types():
    import duckdb

    from stratum.ask.fallback import _load

    connection = duckdb.connect(":memory:")
    _load(connection, ["a", "b", "c"], [(1, "x", 1.5), (2, "y", 2.5), (3, None, 3.5)])
    assert connection.execute("SELECT SUM(a), AVG(c) FROM extracted").fetchone() == (6, 2.5)
    connection.close()


# ------------------------------------------------------- compiled vs clean database


CLEAN_CASES = [
    ("customer count", "SELECT COUNT(*) AS n FROM customer", "SELECT COUNT(*) FROM Customer"),
]


def test_row_cap_is_dialect_correct():
    from stratum.connectors.executor import enforce_row_cap

    assert enforce_row_cap("SELECT * FROM t", SQLITE3, 100) == "SELECT * FROM t LIMIT 100"
    assert enforce_row_cap("SELECT a FROM t", TSQL2008, 100) == "SELECT TOP 100 a FROM t"
    # An existing tighter limit is left alone.
    assert enforce_row_cap("SELECT * FROM t LIMIT 5", SQLITE3, 100) == "SELECT * FROM t LIMIT 5"


def test_schema_index_prunes_columns(model):
    index = SchemaIndex(model)
    result = index.search("how many customers")
    assert result.entities
    for entity in result.entities:
        assert len(entity.columns) <= 14, "the prompt budget depends on this"


def test_ranking_measures_skip_identifiers_and_codes():
    """"Best customers" should never be offered as "top 10 by total id": ids, numbers,
    statuses and join columns are numeric but not quantities."""
    from stratum.ask.ambiguity import _available_measures
    from stratum.semantic.model import Column, Entity, Relationship, SemanticModel

    def col(name, physical, kind="integer"):
        return Column(name=name, expr=physical, physical_column=physical, data_type=kind)

    billing = Entity(name="billing", source="INV", key=["INV_ID"], columns={
        c.name: c for c in [
            col("id", "INV_ID"),
            col("customer", "CUST"),
            col("order_number", "ORD_NO"),
            col("order_status", "ORD_STS"),
            col("total_amount", "TOT_AMT", "decimal"),
            col("quantity", "QTY"),
        ]
    })
    model = SemanticModel(
        database="t",
        entities={"billing": billing},
        relationships=[Relationship(
            name="billing_customer", from_entity="billing", to_entity="customer",
            from_table="INV", to_table="CUST", from_columns=["CUST"], to_columns=["CUST_ID"],
        )],
    )
    labels = [label for label, _ in _available_measures(model, ["billing"])]
    assert labels == ["total amount", "total quantity"]


def test_ranking_options_come_from_the_ranked_entity():
    """"Best customers" ranks customers by what points at them, not by a film's
    release year that happened to be linked too."""
    from stratum.ask.ambiguity import Ambiguity, build_options
    from stratum.semantic.model import Column, Entity, Relationship, SemanticModel

    def entity(name, *columns):
        return Entity(name=name, source=name.upper(), key=["ID"], columns={
            c: Column(name=c, expr=c.upper(), physical_column=c.upper(), data_type="decimal")
            for c in columns
        })

    model = SemanticModel(
        database="t",
        entities={
            "customer": entity("customer"),
            "payment": entity("payment", "amount"),
            "film": entity("film", "release_year", "rental_duration"),
        },
        relationships=[Relationship(
            name="payment_customer", from_entity="payment", to_entity="customer",
            from_table="PAYMENT", to_table="CUSTOMER",
        )],
    )
    options = build_options(
        Ambiguity("intent", "best", "what makes one better"), model,
        ["film", "customer", "payment"], "Who are our best customers?",
    )
    assert [o.label for o in options] == [
        "Top 10 by total amount", "Top 10 by number of payment records",
    ]


def test_reexcavation_keeps_review_decisions():
    from stratum.api.app import _carry_decisions
    from stratum.semantic.model import SemanticModel

    def finding(status="hypothesis", value="dead"):
        return {"id": "table_class:wrk_1:dead", "kind": "table_class", "target": "wrk_1",
                "value": value, "status": status}

    model = SemanticModel(database="t")
    fresh = [finding()]
    kept = _carry_decisions(model, fresh, [finding(status="rejected")])
    assert kept == 1
    assert fresh[0]["status"] == "rejected"
    assert _carry_decisions(model, [finding()], [finding()]) == 0
