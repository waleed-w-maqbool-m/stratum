"""SQLite connector.

Opened read-only via the URI form (`file:...?mode=ro`) so the file cannot be modified
even if a guard somewhere above were to fail. The statement timeout is implemented
with `set_progress_handler`, which is the only interrupt mechanism sqlite3 offers.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

import sqlglot

from .base import (
    ColumnInfo,
    Connector,
    CostEstimate,
    ForeignKeyInfo,
    IndexInfo,
    Privileges,
    RoutineInfo,
    ServerInfo,
    TableInfo,
)
from .profiles import SQLITE3, CapabilityProfile

_INTERNAL = re.compile(r"^(sqlite_|_capped)", re.IGNORECASE)


class SQLiteConnector(Connector):
    dialect = "sqlite"

    ERROR_HINTS = [
        ("unable to open database file",
         "SQLite could not open that file — check the path, and that the file exists."),
        ("file is not a database",
         "That file is not a SQLite database. If it is a .sql dump, load it first."),
        ("no such table",
         "The query references a table this database does not have."),
        ("attempt to write a readonly database",
         "Something tried to write. Stratum opens SQLite read-only by design."),
    ]

    def __init__(self, url: str, profile: CapabilityProfile | None = None):
        super().__init__(url, profile or SQLITE3)
        from .base import parse_url

        self.path = Path(parse_url(url)["path"]).expanduser()
        self.conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"SQLite file not found: {self.path}")
        uri = f"file:{self.path.as_posix()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.text_factory = lambda b: b.decode("utf-8", "replace")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _require(self) -> sqlite3.Connection:
        if self.conn is None:
            self.connect()
        assert self.conn is not None
        return self.conn

    def server_info(self) -> ServerInfo:
        conn = self._require()
        version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        return ServerInfo(
            dialect="sqlite",
            version=version,
            database=self.path.name,
            profile=self._profile or SQLITE3,
            privileges=Privileges(
                can_write=False,
                can_ddl=False,
                username=os.getlogin() if hasattr(os, "getlogin") else "",
                notes=["opened read-only via file: URI"],
            ),
        )

    # ------------------------------------------------------------------ execution
    def execute_readonly(
        self, sql: str, *, timeout_ms: int, row_cap: int
    ) -> tuple[list[str], list[tuple]]:
        conn = self._require()
        deadline = time.perf_counter() + timeout_ms / 1000.0

        def _watchdog() -> int:
            # Returning non-zero aborts the running statement.
            return 1 if time.perf_counter() > deadline else 0

        conn.set_progress_handler(_watchdog, 2000)
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            rows = cursor.fetchmany(row_cap)
            cursor.close()
            return columns, [tuple(r) for r in rows]
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise TimeoutError(f"query exceeded {timeout_ms} ms") from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)
            # SQLite autocommits reads; the read-only URI is what guarantees no write.
            conn.rollback()

    def explain(self, sql: str) -> CostEstimate:
        conn = self._require()
        try:
            plan = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        except sqlite3.Error as exc:
            return CostEstimate(supported=False, plan_text=str(exc))

        text = "\n".join(str(row[-1]) for row in plan)
        scans = re.findall(r"SCAN (?:TABLE )?(\w+)", text)
        counts = self._row_estimates()
        scanned_rows = sum(counts.get(t.lower(), 0) for t in scans)
        return CostEstimate(
            estimated_rows=float(scanned_rows),
            estimated_cost=float(scanned_rows),
            # SQLite reads roughly a million rows a second on a warm cache; this is a
            # deliberately rough order-of-magnitude figure for the cost guard, not a
            # prediction.
            estimated_ms=scanned_rows / 1000.0 if scanned_rows else 0.0,
            scans=scans,
            plan_text=text,
        )

    _estimates: dict[str, int] | None = None

    def _row_estimates(self) -> dict[str, int]:
        """SQLite has no catalog statistics, so count once and cache.

        Counting is honest but slow on a big table; it happens at most once per
        connection and the numbers are what the cost guard reasons about.
        """
        if self._estimates is not None:
            return self._estimates
        conn = self._require()
        estimates: dict[str, int] = {}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            if _INTERNAL.match(name):
                continue
            try:
                estimates[name.lower()] = conn.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0]
            except sqlite3.Error:
                estimates[name.lower()] = 0
        self._estimates = estimates
        return estimates

    # ---------------------------------------------------------------- introspection
    def list_tables(self) -> list[TableInfo]:
        conn = self._require()
        counts = self._row_estimates()
        out: list[TableInfo] = []
        for name, kind, sql in conn.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') "
            "ORDER BY name"
        ):
            if _INTERNAL.match(name):
                continue
            out.append(
                TableInfo(
                    schema="",
                    name=name,
                    kind="view" if kind == "view" else "table",
                    row_estimate=counts.get(name.lower(), 0),
                    definition=sql if kind == "view" else None,
                )
            )
        return out

    def list_columns(self) -> list[ColumnInfo]:
        conn = self._require()
        out: list[ColumnInfo] = []
        for table in self.list_tables():
            try:
                info = conn.execute(f'PRAGMA table_info("{table.name}")').fetchall()
            except sqlite3.Error:
                continue  # a view over dropped columns cannot be described
            for cid, name, decl_type, notnull, default, _pk in info:
                char_len = None
                match = re.search(r"\((\d+)", decl_type or "")
                if match:
                    char_len = int(match.group(1))
                out.append(
                    ColumnInfo(
                        table=table.name,
                        schema="",
                        name=name,
                        data_type=(decl_type or "").upper() or "BLOB",
                        nullable=not notnull,
                        ordinal=cid,
                        default=default,
                        char_length=char_len,
                    )
                )
        return out

    def list_indexes(self) -> list[IndexInfo]:
        conn = self._require()
        out: list[IndexInfo] = []
        for table in self.list_tables():
            if table.kind == "view":
                continue
            try:
                pk_cols = [
                    row[1]
                    for row in conn.execute(f'PRAGMA table_info("{table.name}")')
                    if row[5]
                ]
            except sqlite3.Error:
                continue
            if pk_cols:
                out.append(
                    IndexInfo("", table.name, f"pk_{table.name}", pk_cols, True, True)
                )
            for _seq, name, unique, _origin, _partial in conn.execute(
                f'PRAGMA index_list("{table.name}")'
            ):
                cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")') if r[2]]
                if cols:
                    out.append(IndexInfo("", table.name, name, cols, bool(unique), False))
        return out

    def list_foreign_keys(self) -> list[ForeignKeyInfo]:
        conn = self._require()
        out: list[ForeignKeyInfo] = []
        for table in self.list_tables():
            if table.kind == "view":
                continue
            try:
                rows = conn.execute(f'PRAGMA foreign_key_list("{table.name}")').fetchall()
            except sqlite3.Error:
                continue
            grouped: dict[int, list] = {}
            for row in rows:
                grouped.setdefault(row[0], []).append(row)
            for _fk_id, members in grouped.items():
                out.append(
                    ForeignKeyInfo(
                        schema="",
                        table=table.name,
                        columns=[m[3] for m in members],
                        ref_schema="",
                        ref_table=members[0][2],
                        ref_columns=[m[4] for m in members],
                    )
                )
        return out

    def list_routines(self) -> list[RoutineInfo]:
        """SQLite has no stored procedures; views are the only harvestable source."""
        conn = self._require()
        out: list[RoutineInfo] = []
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql IS NOT NULL"
        ):
            out.append(RoutineInfo(schema="", name=name, kind="view", definition=sql))
        # Triggers occasionally encode business rules too.
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL"
        ):
            out.append(RoutineInfo(schema="", name=name, kind="trigger", definition=sql))
        return out

    def sample_sql_tail(
        self, schema: str, table: str, columns: list[str], limit: int
    ) -> str | None:
        cols = ", ".join(self.quote(c) for c in columns) if columns else "*"
        return f"SELECT {cols} FROM {self.quote(table)} ORDER BY rowid DESC LIMIT {limit}"

    # --------------------------------------------------------------------- helpers
    def transpile(self, sql: str, read: str = "") -> str:
        return sqlglot.transpile(sql, read=read or None, write="sqlite")[0]
