"""Connector interface.

A connector knows how to reach one database engine and how to answer the handful of
questions the excavation pipeline asks of a live server: what is here, what does it
look like, and what would this query cost. Everything above the connector is engine
agnostic and talks in terms of the typed structures defined here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .profiles import CapabilityProfile


@dataclass
class ColumnInfo:
    table: str
    schema: str
    name: str
    data_type: str
    nullable: bool
    ordinal: int
    default: str | None = None
    is_identity: bool = False
    char_length: int | None = None
    numeric_precision: int | None = None
    numeric_scale: int | None = None
    comment: str | None = None

    @property
    def qualified_table(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    @property
    def qualified(self) -> str:
        return f"{self.qualified_table}.{self.name}"


@dataclass
class TableInfo:
    schema: str
    name: str
    kind: str = "table"           # table | view
    row_estimate: int = 0
    comment: str | None = None
    definition: str | None = None  # view / procedure source text

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class IndexInfo:
    schema: str
    table: str
    name: str
    columns: list[str]
    is_unique: bool = False
    is_primary: bool = False

    @property
    def qualified_table(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table


@dataclass
class RoutineInfo:
    """A stored procedure, function or view body. `procmine.py` reads these."""

    schema: str
    name: str
    kind: str          # view | procedure | function
    definition: str


@dataclass
class ForeignKeyInfo:
    schema: str
    table: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    name: str = ""


@dataclass
class Privileges:
    can_write: bool = False
    can_ddl: bool = False
    username: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class ServerInfo:
    dialect: str
    version: str
    database: str
    profile: CapabilityProfile
    privileges: Privileges


@dataclass
class CostEstimate:
    estimated_rows: float = 0.0
    estimated_cost: float = 0.0
    estimated_ms: float | None = None
    scans: list[str] = field(default_factory=list)      # tables predicted to be scanned
    plan_text: str = ""
    supported: bool = True


class Connector(ABC):
    """One live database connection.

    Implementations must keep `execute_readonly` inside a rolled-back transaction.
    Nothing above this layer is allowed to open a cursor of its own.
    """

    dialect: str = ""

    def __init__(self, url: str, profile: CapabilityProfile | None = None):
        self.url = url
        self._profile = profile

    # ------------------------------------------------------------------ lifecycle
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def server_info(self) -> ServerInfo: ...

    # ------------------------------------------------------------------ execution
    @abstractmethod
    def execute_readonly(
        self, sql: str, *, timeout_ms: int, row_cap: int
    ) -> tuple[list[str], list[tuple]]:
        """Run `sql` inside a transaction that is rolled back afterwards.

        The caller has already proved the statement is a single SELECT.
        """

    @abstractmethod
    def explain(self, sql: str) -> CostEstimate: ...

    # ---------------------------------------------------------------- introspection
    @abstractmethod
    def list_tables(self) -> list[TableInfo]: ...

    @abstractmethod
    def list_columns(self) -> list[ColumnInfo]: ...

    @abstractmethod
    def list_indexes(self) -> list[IndexInfo]: ...

    @abstractmethod
    def list_foreign_keys(self) -> list[ForeignKeyInfo]: ...

    @abstractmethod
    def list_routines(self) -> list[RoutineInfo]: ...

    # --------------------------------------------------------------------- helpers
    @property
    def profile(self) -> CapabilityProfile:
        if self._profile is None:
            self._profile = self.server_info().profile
        return self._profile

    def quote(self, *parts: str) -> str:
        p = self.profile
        return ".".join(f"{p.quote_open}{part}{p.quote_close}" for part in parts if part)

    def sample_sql(self, schema: str, table: str, columns: list[str], limit: int) -> str:
        """Dialect-correct 'give me n rows' — TOP for old SQL Server, LIMIT elsewhere."""
        cols = ", ".join(self.quote(c) for c in columns) if columns else "*"
        target = self.quote(schema, table) if schema else self.quote(table)
        if self.profile.top:
            return f"SELECT TOP {limit} {cols} FROM {target}"
        return f"SELECT {cols} FROM {target} LIMIT {limit}"

    def sample_sql_tail(
        self, schema: str, table: str, columns: list[str], limit: int
    ) -> str | None:
        """The same, from the other end of the table.

        A head-only sample is systematically biased towards the oldest rows, which
        hides exactly the things excavation is looking for: a column added in a later
        migration looks empty, a column retired in an earlier one looks full, and a
        child table's leading key column looks constant because the first 2000 rows
        all belong to one parent.

        Returns None when the engine offers no cheap way to read from the end; the
        caller then profiles the head alone.
        """
        return None

    def friendly_error(self, exc: Exception) -> str:
        """Map a driver error onto something a human can act on."""
        text = str(exc)
        for needle, hint in self.ERROR_HINTS:
            if needle.lower() in text.lower():
                return hint
        return text

    ERROR_HINTS: list[tuple[str, str]] = []


def parse_url(url: str) -> dict[str, Any]:
    """Split a database URL into the fields the Connect page shows the user.

    Deliberately lenient: the point is to echo back what was understood so the user
    can correct it, not to validate.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    dialect = {
        "postgresql": "postgres", "postgres": "postgres", "psql": "postgres",
        "mssql": "mssql", "sqlserver": "mssql", "mssql+pymssql": "mssql",
        "sqlite": "sqlite", "file": "sqlite",
    }.get(scheme, scheme)

    if dialect == "sqlite":
        path = url.split("///", 1)[-1] if "///" in url else parsed.path
        return {"dialect": "sqlite", "path": unquote(path), "database": unquote(path)}

    default_port = {"postgres": 5432, "mssql": 1433}.get(dialect)
    return {
        "dialect": dialect,
        "host": parsed.hostname or "",
        "port": parsed.port or default_port,
        "username": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
        "database": parsed.path.lstrip("/") if parsed.path else "",
        "options": parsed.query,
    }
