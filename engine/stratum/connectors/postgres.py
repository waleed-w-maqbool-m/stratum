"""PostgreSQL connector (psycopg 3).

Read-only is enforced three ways: the statement guard above, a session set to
`default_transaction_read_only`, and an explicit ROLLBACK after every statement.
"""

from __future__ import annotations

import re
from typing import Any

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
from .profiles import PG16, CapabilityProfile

# Schemas that are never a user's data. Passed as a list and matched with `<> ALL`:
# psycopg 3 does not expand a tuple into `NOT IN (...)` the way psycopg 2 did, so the
# older spelling failed on the very first catalog query.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


class PostgresConnector(Connector):
    dialect = "postgres"

    ERROR_HINTS = [
        ("password authentication failed",
         "Postgres rejected the username/password."),
        ("could not connect to server",
         "Nothing is listening at that host and port — is the container running?"),
        ("database \"", "That database does not exist on this server."),
        ("permission denied",
         "The login cannot read that object. Stratum needs SELECT and catalog access."),
        ("SSL connection", "The server requires SSL; add ?sslmode=require to the URL."),
    ]

    def __init__(self, url: str, profile: CapabilityProfile | None = None):
        super().__init__(url, profile or PG16)
        self.conn: Any = None

    def connect(self) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "psycopg is not installed. Install the extra: uv sync --extra postgres"
            ) from exc
        self.conn = psycopg.connect(self.url, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = 15000")
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _require(self):
        # A connection the server closed is `broken`, not None. Reconnect rather than
        # fail: an eval holds one connection for an hour, and a restart or an admin's
        # terminate should cost one reconnect, not the rest of the run.
        if self.conn is None or self.conn.closed or getattr(self.conn, "broken", False):
            self.connect()
        return self.conn

    def _retrying(self, operation):
        """Run a read once more on a fresh connection if the connection itself died.

        Safe because every statement here is a read that is rolled back: retrying it
        cannot apply anything twice. Only a lost connection is retried — a bad query
        fails exactly as before.
        """
        import psycopg

        try:
            return operation()
        except psycopg.OperationalError:
            if self.conn is not None and not (
                self.conn.closed or getattr(self.conn, "broken", False)
            ):
                raise
            self.conn = None
            return operation()

    def server_info(self) -> ServerInfo:
        conn = self._require()
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database(), current_user")
            version, database, user = cur.fetchone()
            # Warn if the login could write, even though we never will.
            cur.execute(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
            )
            can_ddl = bool(cur.fetchone()[0])
            cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
            row = cur.fetchone()
            is_super = bool(row[0]) if row else False
        conn.rollback()
        notes = []
        if can_ddl or is_super:
            notes.append(
                "This login can create objects. Stratum will never write, but a "
                "read-only role is the safer thing to hand it."
            )
        return ServerInfo(
            dialect="postgres",
            version=version.split(",")[0],
            database=database,
            profile=self._profile or PG16,
            privileges=Privileges(
                can_write=can_ddl or is_super, can_ddl=can_ddl, username=user, notes=notes
            ),
        )

    def execute_readonly(
        self, sql: str, *, timeout_ms: int, row_cap: int
    ) -> tuple[list[str], list[tuple]]:
        return self._retrying(lambda: self._execute_once(sql, timeout_ms, row_cap))

    def _execute_once(
        self, sql: str, timeout_ms: int, row_cap: int
    ) -> tuple[list[str], list[tuple]]:
        conn = self._require()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                cur.execute(sql)
                columns = [d.name for d in (cur.description or [])]
                rows = cur.fetchmany(row_cap)
            return columns, [tuple(r) for r in rows]
        finally:
            conn.rollback()  # always; the transaction never commits

    def explain(self, sql: str) -> CostEstimate:
        conn = self._require()
        try:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                plan = cur.fetchone()[0]
        except Exception as exc:
            conn.rollback()
            return CostEstimate(supported=False, plan_text=str(exc))
        finally:
            conn.rollback()

        root = plan[0]["Plan"] if isinstance(plan, list) else plan["Plan"]
        scans: list[str] = []

        def walk(node: dict) -> None:
            if node.get("Node Type", "").endswith("Seq Scan"):
                scans.append(node.get("Relation Name", "?"))
            for child in node.get("Plans", []):
                walk(child)

        walk(root)
        return CostEstimate(
            estimated_rows=float(root.get("Plan Rows", 0)),
            estimated_cost=float(root.get("Total Cost", 0)),
            estimated_ms=None,
            scans=scans,
            plan_text=str(plan),
        )

    def sample_sql_tail(
        self, schema: str, table: str, columns: list[str], limit: int
    ) -> str | None:
        cols = ", ".join(self.quote(c) for c in columns) if columns else "*"
        target = self.quote(schema, table) if schema else self.quote(table)
        return f"SELECT {cols} FROM {target} ORDER BY ctid DESC LIMIT {limit}"

    # ---------------------------------------------------------------- introspection
    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self._retrying(lambda: self._query_once(sql, params))

    def _query_once(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._require()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [tuple(r) for r in cur.fetchall()]
        finally:
            conn.rollback()

    def list_tables(self) -> list[TableInfo]:
        rows = self._query(
            """
            SELECT n.nspname, c.relname,
                   CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'view' ELSE 'table' END,
                   COALESCE(c.reltuples, 0)::bigint,
                   obj_description(c.oid, 'pg_class'),
                   CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid, true) END
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','p','v','m','f')
              -- A partition is storage, not a table anyone queries by name: the
              -- parent is the relation, and listing every monthly child would hand
              -- the model seven copies of the same entity.
              AND NOT c.relispartition
              AND n.nspname <> ALL(%s)
            ORDER BY n.nspname, c.relname
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        return [
            TableInfo(schema=s, name=t, kind=k, row_estimate=max(int(n), 0),
                      comment=c, definition=d)
            for s, t, k, n, c, d in rows
        ]

    def list_columns(self) -> list[ColumnInfo]:
        rows = self._query(
            """
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
                   c.is_nullable = 'YES', c.ordinal_position, c.column_default,
                   c.is_identity = 'YES', c.character_maximum_length,
                   c.numeric_precision, c.numeric_scale,
                   col_description(
                     format('%%I.%%I', c.table_schema, c.table_name)::regclass::oid,
                     c.ordinal_position)
            FROM information_schema.columns c
            JOIN pg_catalog.pg_class pc ON pc.relname = c.table_name
            JOIN pg_catalog.pg_namespace pn
              ON pn.oid = pc.relnamespace AND pn.nspname = c.table_schema
            WHERE c.table_schema <> ALL(%s)
              AND NOT pc.relispartition
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        return [
            ColumnInfo(
                schema=s, table=t, name=n, data_type=dt, nullable=null, ordinal=pos,
                default=dflt, is_identity=ident, char_length=clen,
                numeric_precision=prec, numeric_scale=scale, comment=comment,
            )
            for s, t, n, dt, null, pos, dflt, ident, clen, prec, scale, comment in rows
        ]

    def list_indexes(self) -> list[IndexInfo]:
        rows = self._query(
            """
            SELECT n.nspname, t.relname, i.relname, ix.indisunique, ix.indisprimary,
                   -- attname, not pg_get_indexdef: the latter renders SQL and so quotes
                   -- any mixed-case name, and a primary key recorded as '"intFilmID"'
                   -- never matches the column intFilmID. Expression index members have
                   -- attnum 0, join to nothing, and drop out as they should.
                   ARRAY(SELECT a.attname
                         FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                         JOIN pg_attribute a
                           ON a.attrelid = ix.indrelid AND a.attnum = k.attnum
                         ORDER BY k.ord)
            FROM pg_index ix
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname <> ALL(%s)
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        return [
            IndexInfo(schema=s, table=t, name=i, columns=list(cols),
                      is_unique=uniq, is_primary=prim)
            for s, t, i, uniq, prim, cols in rows
        ]

    def list_foreign_keys(self) -> list[ForeignKeyInfo]:
        rows = self._query(
            """
            SELECT con.conname, n.nspname, cl.relname,
                   ARRAY(SELECT a.attname FROM unnest(con.conkey) k
                         JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attnum = k),
                   fn.nspname, fcl.relname,
                   ARRAY(SELECT a.attname FROM unnest(con.confkey) k
                         JOIN pg_attribute a ON a.attrelid = fcl.oid AND a.attnum = k),
                   con.convalidated
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = cl.relnamespace
            JOIN pg_class fcl ON fcl.oid = con.confrelid
            JOIN pg_namespace fn ON fn.oid = fcl.relnamespace
            WHERE con.contype = 'f' AND n.nspname <> ALL(%s)
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        return [
            ForeignKeyInfo(name=name, schema=s, table=t, columns=list(cols),
                           ref_schema=fs, ref_table=ft, ref_columns=list(fcols),
                           enforced=bool(valid))
            for name, s, t, cols, fs, ft, fcols, valid in rows
        ]

    def list_routines(self) -> list[RoutineInfo]:
        rows = self._query(
            """
            SELECT n.nspname, p.proname,
                   CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END,
                   pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname <> ALL(%s) AND p.prokind IN ('f','p')
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        out = [
            RoutineInfo(schema=s, name=n, kind=k, definition=d or "")
            for s, n, k, d in rows
        ]
        out += [
            RoutineInfo(schema=t.schema, name=t.name, kind="view", definition=t.definition)
            for t in self.list_tables()
            if t.kind == "view" and t.definition
        ]
        return out


def normalise_type(pg_type: str) -> str:
    """Collapse Postgres type spellings onto the handful the detectors care about."""
    t = re.sub(r"\(.*\)", "", pg_type).strip().lower()
    if t in ("integer", "bigint", "smallint", "int", "int2", "int4", "int8"):
        return "int"
    if t.startswith(("numeric", "decimal", "double", "real", "float", "money")):
        return "decimal"
    if t.startswith(("character varying", "varchar", "text", "char", "citext")):
        return "text"
    if t.startswith(("timestamp", "date", "time")):
        return "datetime"
    if t in ("boolean", "bool"):
        return "bool"
    if t in ("json", "jsonb"):
        return "json"
    if t == "xml":
        return "xml"
    return t
