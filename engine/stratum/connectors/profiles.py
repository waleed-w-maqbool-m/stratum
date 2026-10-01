"""Dialect capability profiles.

A profile is a set of feature flags plus a sqlglot write dialect. It is what makes
"this SQL Server is running 2022 but the shop only allows 2008-era syntax" an
enforceable, testable statement rather than a convention nobody follows.

Profiles are law (CLAUDE.md non-negotiable #5): `dialect.py` lints every compiled
query against the connection's profile and refuses to hand a driver something the
profile forbids.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal

Feature = Literal[
    "cte",
    "window_basic",
    "window_frame",
    "lag_lead",
    "offset_fetch",
    "limit",
    "top",
    "json_fns",
    "xml_value",
    "try_cast",
    "concat_fn",
    "string_agg",
    "iif",
    "ilike",
    "regex",
    "except_intersect",
    "full_outer_join",
    "boolean_type",
]


@dataclass(frozen=True)
class CapabilityProfile:
    """What a connection is allowed to emit.

    `sqlglot_dialect` is the write dialect; `read_dialect` defaults to it but can
    differ when we parse logical SQL in one dialect and emit in another.
    """

    name: str
    sqlglot_dialect: str
    description: str = ""

    cte: bool = True
    window_basic: bool = True          # ROW_NUMBER / RANK with PARTITION BY + ORDER BY
    window_frame: bool = True          # ROWS/RANGE BETWEEN
    lag_lead: bool = True
    offset_fetch: bool = True          # OFFSET n ROWS FETCH NEXT m ROWS ONLY
    limit: bool = True                 # LIMIT n
    top: bool = False                  # SELECT TOP n
    json_fns: bool = True
    xml_value: bool = False
    try_cast: bool = True
    concat_fn: bool = True             # CONCAT(a, b)
    string_agg: bool = True
    iif: bool = True
    ilike: bool = False
    regex: bool = False
    except_intersect: bool = True
    full_outer_join: bool = True
    boolean_type: bool = True

    # Execution limits applied by the read-only executor.
    statement_timeout_ms: int = 15_000
    row_cap: int = 5_000

    # Identifier quoting, used when compiling physical SQL by hand.
    quote_open: str = '"'
    quote_close: str = '"'

    def allows(self, feature: str) -> bool:
        return bool(getattr(self, feature, False))

    def forbidden(self) -> list[str]:
        """Every feature flag that is off, for error messages and prompts."""
        skip = {"name", "sqlglot_dialect", "description", "statement_timeout_ms",
                "row_cap", "quote_open", "quote_close"}
        return sorted(
            f.name for f in fields(self)
            if f.name not in skip and f.type is bool and not getattr(self, f.name)
        )

    def quote(self, identifier: str) -> str:
        return f"{self.quote_open}{identifier}{self.quote_close}"


# --------------------------------------------------------------------------- registry

TSQL2008 = CapabilityProfile(
    name="tsql2008",
    sqlglot_dialect="tsql",
    description="SQL Server 2008-era syntax. The container may be 2022; the shop is not.",
    cte=True,
    window_basic=True,        # ROW_NUMBER() OVER (...) shipped in 2005
    window_frame=False,       # ROWS/RANGE BETWEEN arrived in 2012
    lag_lead=False,           # 2012
    offset_fetch=False,       # 2012
    limit=False,
    top=True,
    json_fns=False,           # 2016
    xml_value=True,           # .value('...', '...') is the 2008 way
    try_cast=False,           # TRY_CAST / TRY_CONVERT arrived in 2012
    concat_fn=False,          # CONCAT arrived in 2012; use + with ISNULL
    string_agg=False,         # 2017
    iif=False,                # 2012
    ilike=False,
    regex=False,
    quote_open="[",
    quote_close="]",
)

TSQL2016 = CapabilityProfile(
    name="tsql2016",
    sqlglot_dialect="tsql",
    description="SQL Server 2016+.",
    limit=False,
    top=True,
    xml_value=True,
    ilike=False,
    regex=False,
    quote_open="[",
    quote_close="]",
)

PG16 = CapabilityProfile(
    name="pg16",
    sqlglot_dialect="postgres",
    description="PostgreSQL 16.",
    top=False,
    xml_value=True,
    iif=False,                # Postgres has no IIF; CASE instead
    ilike=True,
    regex=True,
)

SQLITE3 = CapabilityProfile(
    name="sqlite3",
    sqlglot_dialect="sqlite",
    description="SQLite 3.35+ (window functions, but no server-side types).",
    offset_fetch=False,
    top=False,
    json_fns=True,
    xml_value=False,          # no XML support at all; XML must be parsed with string ops
    try_cast=False,           # CAST never raises in SQLite, but TRY_CAST is not a keyword
    concat_fn=False,          # use || ; CONCAT() only landed in 3.44
    string_agg=False,         # GROUP_CONCAT instead
    iif=True,                 # 3.32+
    ilike=False,              # LIKE is case-insensitive for ASCII anyway
    regex=False,              # REGEXP needs a loaded extension
    boolean_type=False,
    quote_open="[",
    quote_close="]",
)

# Artificial profile used to exercise the rewriter: a Postgres/SQLite server that we
# pretend cannot do CTEs or window functions at all. PLAN.md 3.1 runs pagila under it.
LEGACY_STRICT = CapabilityProfile(
    name="legacy_strict",
    sqlglot_dialect="postgres",
    description="Deliberately impoverished: no CTEs, no window functions, no JSON.",
    cte=False,
    window_basic=False,
    window_frame=False,
    lag_lead=False,
    offset_fetch=False,
    limit=True,
    top=False,
    json_fns=False,
    try_cast=False,
    concat_fn=False,
    string_agg=False,
    iif=False,
    except_intersect=False,
    full_outer_join=False,
)

PROFILES: dict[str, CapabilityProfile] = {
    p.name: p for p in (TSQL2008, TSQL2016, PG16, SQLITE3, LEGACY_STRICT)
}


def get(name: str) -> CapabilityProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(
            f"unknown capability profile {name!r}; known: {sorted(PROFILES)}"
        ) from None


def for_server(dialect: str, version: str | None) -> CapabilityProfile:
    """Pick a default profile from a detected server version.

    SQL Server deliberately defaults to the *2008* profile rather than to whatever the
    server reports. Legacy shops run modern servers at old compatibility levels, and
    being too permissive here produces SQL that works in the container and fails in
    production. Widening is an explicit choice the user makes on the connection.
    """
    dialect = dialect.lower()
    if dialect in ("mssql", "tsql", "sqlserver"):
        return TSQL2008
    if dialect in ("postgres", "postgresql", "pg"):
        return PG16
    if dialect == "sqlite":
        return SQLITE3
    raise KeyError(f"no default profile for dialect {dialect!r}")
