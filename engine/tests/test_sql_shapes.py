"""SQL shapes a generator legitimately produces, which the pipeline must not refuse.

Every case here was found by an eval run rejecting correct SQL. They are collected as
one file because they share a cause: the compiler and validator were modelling *the
model's output* more narrowly than SQL actually allows, so a correct query was sent to
the repair loop — or abandoned — for using a construct the plan explicitly invites.

PLAN.md's risk table calls for "a regression file of fixed cases". This is it.
"""

from __future__ import annotations

import pytest

from stratum.ask import dialect as dialect_module
from stratum.ask.compiler import compile_logical
from stratum.ask.validate import validate
from stratum.connectors.profiles import SQLITE3, TSQL2008


def _entity_names(model):
    return sorted(model.entities)


# --------------------------------------------------------------- scoping and aliases


def test_projection_alias_in_group_by(model, connector, executor):
    """`SELECT ... AS yr ... GROUP BY yr` is valid SQL everywhere."""
    compiled = compile_logical(
        "SELECT customer.cfnm AS who, COUNT(*) AS n FROM customer GROUP BY who",
        model, connector.profile,
    )
    check = next(
        c for c in validate(compiled, model, executor, check_fan_out=False).checks
        if c.name == "objects_exist"
    )
    assert check.passed is True, check.message


def test_derived_table_columns_are_in_scope(model, connector, executor):
    compiled = compile_logical(
        "SELECT sub.n FROM (SELECT COUNT(*) AS n FROM customer) AS sub",
        model, connector.profile,
    )
    check = next(
        c for c in validate(compiled, model, executor, check_fan_out=False).checks
        if c.name == "objects_exist"
    )
    assert check.passed is True, check.message


def test_cte_is_not_mistaken_for_an_entity(model, connector, executor):
    """The decompose strategy is told to use a CTE when it clarifies the grain.

    Treating the CTE's name as an entity made the compiler reject every query that
    strategy produced — half the candidates, refused before they ran.
    """
    compiled = compile_logical(
        "WITH live AS (SELECT customer.id AS cid FROM customer) "
        "SELECT COUNT(*) AS n FROM live",
        model, connector.profile,
    )
    assert "CUST_MSTR" in compiled.physical_sql
    result = executor.run(compiled.physical_sql)
    assert result.rows[0][0] == 8, "the entity's soft-delete filter still applies"


def test_a_genuinely_unknown_column_is_still_caught(model, connector, executor):
    """Widening the scope rules must not blind the check to a real hallucination."""
    compiled = compile_logical("SELECT COUNT(*) AS n FROM customer", model, connector.profile)
    compiled.logical_sql = "SELECT customer.invented_column FROM customer"
    check = next(
        c for c in validate(compiled, model, executor, check_fan_out=False).checks
        if c.name == "objects_exist"
    )
    assert check.passed is False
    assert "invented_column" in check.message


def test_a_physical_table_name_is_still_refused(model, connector):
    from stratum.ask.compiler import CompileError

    with pytest.raises(CompileError, match="unknown entity"):
        compile_logical("SELECT COUNT(*) FROM TRANS_MSTR", model, connector.profile)


# ------------------------------------------------------------------- date idioms


def test_extract_is_translated_for_sqlite(model, connector, executor):
    """SQLite has no EXTRACT, and sqlglot passes it through rather than failing — so
    the error arrived at the database instead of at the compiler."""
    compiled = compile_logical(
        "SELECT COUNT(*) AS n FROM billing "
        "WHERE EXTRACT(YEAR FROM billing.order_date_text) = 2022",
        model, connector.profile,
    )
    assert "EXTRACT" not in compiled.physical_sql.upper()
    assert "STRFTIME" in compiled.physical_sql.upper()
    executor.run(compiled.physical_sql)      # must not raise


def test_to_char_format_is_translated_for_sqlite(model, connector, executor):
    """Rendering first loses the format argument and leaves a bare CAST, so the
    rewrite has to happen while the tree is still dialect-neutral."""
    compiled = compile_logical(
        "SELECT TO_CHAR(billing.order_date_text, 'YYYY-MM') AS ym, "
        "COUNT(*) AS n FROM billing GROUP BY ym",
        model, connector.profile,
    )
    assert "STRFTIME" in compiled.physical_sql.upper()
    rows = executor.run(compiled.physical_sql).rows
    assert rows and len(str(rows[0][0])) == 7, f"expected a YYYY-MM bucket, got {rows[:1]}"


def test_an_unrecognised_date_format_is_left_alone():
    """Guessing at a directive we do not know would produce a plausible wrong string."""
    from stratum.ask.dialect import _to_strftime_format

    assert _to_strftime_format("YYYY-MM") == "%Y-%m"
    assert _to_strftime_format("Month") is None


# ------------------------------------------------------- dialect-aware lint rules


def test_sqlite_strftime_is_not_mistaken_for_tsql_format():
    """`FORMAT` and `STRFTIME` are the same sqlglot class. Forbidding the class outright
    rejected SQLite's own date function as a SQL Server 2012 feature."""
    assert dialect_module.lint("SELECT STRFTIME('%Y', c) AS y FROM t", SQLITE3).ok
    assert not dialect_module.lint("SELECT FORMAT(c, 'yyyy') AS y FROM t", TSQL2008).ok


def test_case_is_not_mistaken_for_iif():
    """sqlglot represents a CASE's WHEN branches with the same node as IIF."""
    assert dialect_module.lint(
        "SELECT CASE WHEN a > 1 THEN 'y' ELSE 'n' END AS f FROM t", SQLITE3
    ).ok
