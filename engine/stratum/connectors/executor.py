"""Read-only SQL execution.

CLAUDE.md non-negotiable #1: every user statement is parsed with sqlglot, and anything
that is not a single SELECT is rejected *before it reaches a driver*. Execution then
happens inside a transaction that is always rolled back, under a statement timeout and
a row cap.

The guard is deliberately paranoid in a specific way: it rejects on the parse tree,
never on a regex over the text, and it rejects anything it does not positively
recognise. A statement type sqlglot does not know about is refused, not allowed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .profiles import CapabilityProfile


class ReadOnlyViolation(Exception):
    """The statement is not a single read-only SELECT."""


class ExecutionError(Exception):
    """The database rejected or failed the statement."""


# Expression types that may legally appear as the root of a user statement.
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)

# Anything in this set anywhere in the tree is an immediate refusal, even nested.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Merge, exp.Command, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Use, exp.Set, exp.AlterColumn,
)

# Functions that read or write outside the query itself. sqlglot parses most of these
# as anonymous functions, so they are matched by name.
_FORBIDDEN_FUNCTIONS = {
    # SQL Server
    "openrowset", "openquery", "opendatasource", "openxml", "xp_cmdshell",
    "sp_executesql", "sp_configure", "bulk_insert",
    # Postgres
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_sleep",
    "lo_import", "lo_export", "dblink", "dblink_exec", "copy",
    # SQLite
    "readfile", "writefile", "load_extension", "edit",
}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    elapsed_ms: int = 0
    row_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=False)) for row in self.rows]


def assert_read_only(sql: str, dialect: str = "") -> exp.Expression:
    """Parse `sql` and return the tree, or raise `ReadOnlyViolation`.

    Raises rather than returning a bool so that a caller cannot accidentally ignore
    the result and execute anyway.
    """
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise ReadOnlyViolation("empty statement")

    try:
        statements = sqlglot.parse(text, read=dialect or None)
    except Exception as exc:  # sqlglot raises several unrelated types
        raise ReadOnlyViolation(f"could not parse SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ReadOnlyViolation(
            f"expected exactly one statement, found {len(statements)}; "
            "batching is not allowed"
        )
    tree = statements[0]

    if not isinstance(tree, _ALLOWED_ROOTS):
        raise ReadOnlyViolation(
            f"only SELECT is allowed; this parses as {type(tree).__name__.upper()}"
        )

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise ReadOnlyViolation(
                f"{type(node).__name__.upper()} is not allowed anywhere in a query"
            )
        # `SELECT ... INTO newtable` writes, and parses as a Select with an `into`.
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise ReadOnlyViolation("SELECT ... INTO writes a table and is not allowed")
        if isinstance(node, exp.Anonymous):
            name = (node.this or "").lower() if isinstance(node.this, str) else ""
            if name in _FORBIDDEN_FUNCTIONS:
                raise ReadOnlyViolation(f"function {name}() is not allowed")
        if isinstance(node, exp.Func):
            name = node.sql_name().lower()
            if name in _FORBIDDEN_FUNCTIONS:
                raise ReadOnlyViolation(f"function {name}() is not allowed")

    # A trailing comment can hide a second statement from a naive splitter but not
    # from sqlglot; still, refuse the classic injection shape outright.
    if re.search(r";\s*\S", text):
        raise ReadOnlyViolation("statement separator found inside the query")

    return tree


def enforce_row_cap(sql: str, profile: CapabilityProfile, cap: int | None = None) -> str:
    """Bound a query so it cannot return more than the cap.

    sqlglot normalises both `LIMIT n` and `SELECT TOP n` onto the same `limit` node
    and renders whichever the write dialect uses, so the cap is expressed once here
    and comes out dialect-correct. An existing smaller limit is left alone.
    """
    cap = cap or profile.row_cap
    if not (profile.limit or profile.top):
        return sql  # nothing to express the cap with; the driver's fetch size is the bound
    try:
        tree = sqlglot.parse_one(sql, read=profile.sqlglot_dialect)
    except Exception:
        return sql

    existing = tree.args.get("limit") if isinstance(tree, exp.Query) else None
    if existing is not None:
        try:
            if int(existing.expression.name) <= cap:
                return sql
        except (AttributeError, ValueError):
            return sql  # a non-literal limit; leave it alone rather than guess

    if isinstance(tree, exp.Select):
        return tree.limit(cap).sql(dialect=profile.sqlglot_dialect)
    # A set operation cannot take TOP directly; wrap it.
    wrapped = exp.select("*").from_(exp.Subquery(this=tree, alias="_capped"))
    return wrapped.limit(cap).sql(dialect=profile.sqlglot_dialect)


class ReadOnlyExecutor:
    """Executes vetted SELECTs against a connector, always rolling back.

    The connector supplies the driver-level cursor and the timeout mechanism; this
    class owns the policy.
    """

    def __init__(self, connector, profile: CapabilityProfile):
        self.connector = connector
        self.profile = profile
        self.audit: list[dict[str, Any]] = []
        # Catalog names this dialect only resolves when quoted. Set by the excavation
        # pipeline for its own probes, which are written from catalog names and would
        # otherwise fail on `tblFilm`. Left empty everywhere else, so SQL a model wrote
        # — the naive baseline's included — runs exactly as written.
        self.quoted_identifiers: set[str] = set()

    def run(
        self,
        sql: str,
        *,
        row_cap: int | None = None,
        timeout_ms: int | None = None,
        skip_cap: bool = False,
    ) -> QueryResult:
        cap = row_cap or self.profile.row_cap
        timeout = timeout_ms or self.profile.statement_timeout_ms

        assert_read_only(sql, self.profile.sqlglot_dialect)
        if self.quoted_identifiers:
            from ..ask.dialect import quote_catalog_sql

            sql = quote_catalog_sql(sql, self.quoted_identifiers, self.connector.dialect)
        effective = sql if skip_cap else enforce_row_cap(sql, self.profile, cap)

        started = time.perf_counter()
        # Per-connection audit log of every statement that reaches a driver (PLAN 4.8).
        entry = {"sql": effective, "started": started, "cap": cap, "timeout_ms": timeout}
        self.audit.append(entry)
        try:
            columns, rows = self.connector.execute_readonly(
                effective, timeout_ms=timeout, row_cap=cap + 1
            )
        except ReadOnlyViolation:
            raise
        except Exception as exc:
            entry["error"] = str(exc)
            raise ExecutionError(str(exc)) from exc

        elapsed = int((time.perf_counter() - started) * 1000)
        truncated = len(rows) > cap
        if truncated:
            rows = rows[:cap]
        entry["rows"] = len(rows)
        entry["elapsed_ms"] = elapsed
        return QueryResult(
            columns=list(columns),
            rows=[tuple(r) for r in rows],
            truncated=truncated,
            elapsed_ms=elapsed,
            row_count=len(rows),
            warnings=[f"result truncated to {cap} rows"] if truncated else [],
        )

    def probe(self, sql: str, limit: int = 20) -> QueryResult:
        """A small exploratory query run by the repair loop. Same guard, tighter caps."""
        return self.run(sql, row_cap=limit, timeout_ms=min(self.profile.statement_timeout_ms, 5000))
