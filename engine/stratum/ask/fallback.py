"""The fallback ladder, rungs R3 to R5 (PLAN.md 4.5).

| Rung | Trigger | What happens |
|------|---------|--------------|
| R3 | the dialect cannot express it | pull a bounded result into DuckDB and finish there |
| R4 | the columns are not in the model | query the physical schema directly, at lower confidence |
| R5 | still unsure | abstain, with reasons and the best partial answer |

R3 is the interesting one. On `tsql2008` there are no JSON functions, no `LAG`, no
window frames and no `STRING_AGG` — so a question needing any of those cannot be
answered in the database at all. Rather than refuse, the pipeline runs the part that
*is* expressible, pulls a bounded result set out, and finishes the computation in
DuckDB. The answer is then labelled "computed partly outside the database", because a
number that was calculated somewhere else is a different kind of claim.

The bound matters: pulling an unbounded result set out of a legacy database to compute
a median is how a reporting tool takes production down. Every extraction is capped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from ..connectors.executor import QueryResult, ReadOnlyExecutor
from ..semantic.model import SemanticModel
from . import dialect as dialect_module
from .compiler import select_from, set_select_from

# The most rows R3 will pull out of the database to finish a computation locally.
EXTRACTION_CAP = 50_000


@dataclass
class FallbackResult:
    rung: str                      # R3 | R4 | R5
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    sql_run_in_database: str = ""
    sql_run_locally: str = ""
    rows_extracted: int = 0
    note: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rung": self.rung,
            "ok": self.ok,
            "columns": self.columns,
            "rows": [list(r) for r in self.rows[:200]],
            "sql_run_in_database": self.sql_run_in_database,
            "sql_run_locally": self.sql_run_locally,
            "rows_extracted": self.rows_extracted,
            "note": self.note,
            "reasons": self.reasons,
        }


# ------------------------------------------------------------------------ R3


def needs_local_computation(sql: str, profile) -> list[str]:
    """Which constructs in this query the connection's profile cannot run."""
    result = dialect_module.downgrade(sql, profile)
    return [str(v) for v in result.unresolved]


def split_for_duckdb(
    logical_sql: str, model: SemanticModel, profile
) -> tuple[str, str] | None:
    """Split a query into the part the database can run and the part DuckDB must.

    The split is made at the outermost aggregation or window: everything below it —
    the joins, the filters, the entity expansion — runs in the database, and the part
    the dialect cannot express runs over the extracted rows.

    Returns (inner logical SQL, outer DuckDB SQL over a table called `extracted`), or
    None when the query cannot be split safely.
    """
    try:
        tree = sqlglot.parse_one(logical_sql)
    except Exception:
        return None
    if not isinstance(tree, exp.Select):
        return None

    # The columns the outer computation needs: every column referenced anywhere.
    referenced: list[str] = []
    for column in tree.find_all(exp.Column):
        qualified = f"{column.table}.{column.name}" if column.table else column.name
        if qualified not in referenced:
            referenced.append(qualified)
    if not referenced:
        return None

    def as_column(reference: str) -> exp.Column:
        # `exp.column("a.b")` builds one identifier literally named "a.b"; the qualifier
        # has to be passed separately or the inner query selects a column that does not
        # exist.
        table, _, name = reference.rpartition(".")
        return exp.column(name, table=table or None)

    inner = exp.Select(expressions=[as_column(c) for c in referenced])
    from_clause = select_from(tree)
    if from_clause is None:
        return None
    set_select_from(inner, from_clause.copy())
    if tree.args.get("joins"):
        inner.set("joins", [j.copy() for j in tree.args["joins"]])
    if tree.args.get("where"):
        inner.set("where", tree.args["where"].copy())

    # The outer query is the original, with its FROM replaced by the extracted table
    # and every column reference stripped of its qualifier.
    outer = tree.copy()
    outer.set("joins", None)
    outer.set("where", None)
    set_select_from(outer, exp.From(this=exp.to_table("extracted")))
    for column in outer.find_all(exp.Column):
        if column.table:
            column.set(
                "this", exp.to_identifier(f"{column.table}_{column.name}")
            )
            column.set("table", None)

    # Alias the inner projections to match what the outer query now expects.
    aliased = [
        exp.alias_(as_column(reference), reference.replace(".", "_"), quoted=False)
        for reference in referenced
    ]
    inner.set("expressions", aliased)

    del model, profile
    return inner.sql(), outer.sql(dialect="duckdb")


def run_locally(
    executor: ReadOnlyExecutor,
    inner_physical_sql: str,
    outer_duckdb_sql: str,
    *,
    cap: int = EXTRACTION_CAP,
) -> FallbackResult:
    """Extract a bounded result set and finish the computation in DuckDB."""
    import duckdb

    result = FallbackResult(rung="R3", ok=False, sql_run_in_database=inner_physical_sql,
                            sql_run_locally=outer_duckdb_sql)
    try:
        extracted: QueryResult = executor.run(inner_physical_sql, row_cap=cap)
    except Exception as exc:
        result.reasons.append(f"the extraction query failed: {exc}")
        return result

    result.rows_extracted = extracted.row_count
    if extracted.truncated:
        result.reasons.append(
            f"the extraction hit the {cap:,} row cap, so the local computation is over "
            "a truncated sample and the answer may be wrong"
        )
        return result

    connection = duckdb.connect(":memory:")
    try:
        _load(connection, extracted.columns, extracted.rows)
        cursor = connection.execute(outer_duckdb_sql)
        result.columns = [d[0] for d in (cursor.description or [])]
        result.rows = [tuple(r) for r in cursor.fetchall()]
        result.ok = True
        result.note = (
            f"computed partly outside the database: {extracted.row_count:,} rows were "
            "read from it and the rest of the calculation ran locally, because this "
            "connection's SQL dialect cannot express it"
        )
    except Exception as exc:
        result.reasons.append(f"the local computation failed: {exc}")
    finally:
        connection.close()
    return result


def _load(connection, columns: list[str], rows: list[tuple]) -> None:
    """Create `extracted` in DuckDB and fill it, with types inferred from the values.

    Deliberately avoids arrow and pandas: this rung should not drag a dataframe library
    into the install just to compute a median. Types come from the first non-null value
    in each column, which is enough because the rows all came from one SQL result set
    and are therefore already homogeneous.
    """
    types: list[str] = []
    for index in range(len(columns)):
        sample = next(
            (row[index] for row in rows if row[index] is not None), None
        )
        if isinstance(sample, bool):
            types.append("BOOLEAN")
        elif isinstance(sample, int):
            types.append("BIGINT")
        elif isinstance(sample, float):
            types.append("DOUBLE")
        else:
            types.append("VARCHAR")

    declaration = ", ".join(
        f'"{name}" {sql_type}' for name, sql_type in zip(columns, types, strict=False)
    )
    connection.execute(f"CREATE TABLE extracted ({declaration})")
    if rows:
        placeholders = ", ".join("?" * len(columns))
        connection.executemany(
            f"INSERT INTO extracted VALUES ({placeholders})", [list(r) for r in rows]
        )


# ------------------------------------------------------------------------ R4


@dataclass
class PhysicalSchemaSlice:
    """The raw schema handed to the physical-schema agent when the model falls short."""

    tables: dict[str, list[str]] = field(default_factory=dict)
    samples: dict[str, list[Any]] = field(default_factory=dict)

    def render(self) -> str:
        lines = ["PHYSICAL SCHEMA (raw column names, not logical ones)"]
        for table, columns in self.tables.items():
            lines.append(f"  {table}({', '.join(columns)})")
        return "\n".join(lines)


def physical_slice(
    schema, profiles, table_classes: dict[str, str], *, keywords: list[str],
    limit: int = 8,
) -> PhysicalSchemaSlice:
    """Raw tables whose column names match the question's words.

    R4 exists because the semantic model is a summary, and a summary omits things. When
    a question needs a column the model never mapped, the honest move is to look at the
    physical schema — and to mark the answer's confidence down accordingly, and open a
    Workbench item so the gap gets closed.
    """
    from .linking import tokenize

    wanted = {w for keyword in keywords for w in tokenize(keyword)}
    out = PhysicalSchemaSlice()
    scored: list[tuple[int, Any]] = []

    for table in schema.base_tables:
        if table_classes.get(table.qualified, "core") != "core":
            continue
        tokens = set(tokenize(table.name.replace("_", " ")))
        for column in table.columns:
            tokens |= set(tokenize(column.name.replace("_", " ")))
        overlap = len(tokens & wanted)
        if overlap:
            scored.append((overlap, table))

    scored.sort(key=lambda item: -item[0])
    for _, table in scored[:limit]:
        out.tables[table.qualified] = [c.name for c in table.columns]
        profile = profiles.get(table.qualified)
        if profile is not None:
            for column in table.columns[:12]:
                column_profile = profile.column(column.name)
                if column_profile is not None:
                    out.samples[f"{table.qualified}.{column.name}"] = (
                        column_profile.sample_values(2)
                    )
    return out


# ------------------------------------------------------------------------ R5


def abstain(reasons: list[str], partial: FallbackResult | None = None) -> FallbackResult:
    """The last rung: say what is not known, and show whatever was worked out.

    An abstention that explains itself is useful — it tells the user which mapping to
    fix. An abstention that just says "I could not answer that" is not.
    """
    result = FallbackResult(
        rung="R5",
        ok=False,
        note=(
            "I am not confident enough to answer this. "
            + (reasons[0] if reasons else "")
        ),
        reasons=reasons,
    )
    if partial is not None and partial.rows:
        result.columns = partial.columns
        result.rows = partial.rows
        result.note += (
            "  Below is as far as I got, which may still be useful — but it is not "
            "the answer to what you asked."
        )
    return result
