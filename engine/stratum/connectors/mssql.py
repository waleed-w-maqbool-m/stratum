"""SQL Server connector (pymssql).

Reads the `sys.*` catalog rather than `information_schema`, because the interesting
things for excavation — extended properties, row-count estimates that do not require
a table scan, module definitions — only exist there.

Defaults to the `tsql2008` profile regardless of the server version it finds; see
`profiles.for_server` for why.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from . import mssql_url
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
from .profiles import PROFILES, TSQL2008, CapabilityProfile, for_compat_level, for_server


class MSSQLConnector(Connector):
    dialect = "mssql"

    ERROR_HINTS = [
        ("Login failed for user",
         "SQL Server rejected the username/password — is SQL authentication enabled "
         "on the server, and is the login mapped to this database?"),
        ("Adaptive Server connection failed",
         "Could not reach the server. Check the host and port (1433 by default) and "
         "that TCP/IP is enabled."),
        ("Cannot open database",
         "The login connected but cannot open that database — check the name and that "
         "the login has a user mapped in it."),
        ("The SELECT permission was denied",
         "The login lacks SELECT on that object. Stratum needs SELECT plus VIEW "
         "DEFINITION to read the catalog."),
        ("Data source name not found",
         "No ODBC driver is installed for SQL Server. Install 'ODBC Driver 18 for SQL "
         "Server' from Microsoft, then try again."),
        ("certificate",
         "The server's TLS certificate is not trusted. A local SQL Express instance uses a "
         "self-signed one: add trust_cert=1 to the connection, or Encrypt=False."),
        ("SQL Server Browser",
         "A named instance is found through the SQL Server Browser service (UDP 1434). "
         "Start it, or connect by port instead: Server=host,port."),
        ("Invalid object name",
         "The query references an object that does not exist, or the login cannot see it."),
    ]

    def __init__(self, url: str, profile: CapabilityProfile | None = None):
        super().__init__(url, profile)
        from .base import parse_url

        self.fields = parse_url(url)
        self.conn: Any = None
        self.driver = "pymssql"

    def connect(self) -> None:
        # Windows authentication and named instances (`.\SQLEXPRESS`) are what SSMS users
        # have and pymssql cannot do, so those go through ODBC.
        if mssql_url.needs_odbc(self.fields):
            self._connect_odbc()
            return
        try:
            import pymssql
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "pymssql is not installed. Install the extra: uv sync --extra mssql"
            ) from exc
        self.conn = pymssql.connect(
            server=self.fields["host"],
            port=str(self.fields.get("port") or 1433),
            user=self.fields["username"],
            password=self.fields["password"],
            database=self.fields["database"],
            timeout=30,
            login_timeout=15,
            as_dict=False,
            autocommit=False,
        )

    def _connect_odbc(self) -> None:
        try:
            import pyodbc
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "pyodbc is not installed. Install the extra: uv sync --extra mssql"
            ) from exc
        driver = mssql_url.pick_driver(list(pyodbc.drivers()))
        if driver is None:
            raise RuntimeError(
                "Data source name not found: no ODBC driver for SQL Server is installed. "
                "Install 'ODBC Driver 18 for SQL Server' from Microsoft."
            )
        connection_string = mssql_url.odbc_connection_string(self.fields, driver)
        self.conn = pyodbc.connect(connection_string, timeout=15, autocommit=False)
        self.conn.timeout = 30
        self.driver = driver

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _require(self):
        if self.conn is None:
            self.connect()
        return self.conn

    def server_info(self) -> ServerInfo:
        rows = self._query(
            "SELECT @@VERSION, DB_NAME(), SUSER_SNAME(), "
            "  CAST(SERVERPROPERTY('ProductMajorVersion') AS INT), "
            "  IS_SRVROLEMEMBER('sysadmin'), "
            "  DATABASEPROPERTYEX(DB_NAME(), 'Collation'), "
            "  (SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME())"
        )
        version, database, user, major, sysadmin, _collation, compat = rows[0]
        # Whether the login can write at all — a warning, never a permission we use.
        write_rows = self._query(
            "SELECT COUNT(*) FROM fn_my_permissions(NULL, 'DATABASE') "
            "WHERE permission_name IN ('INSERT','UPDATE','DELETE','ALTER','CONTROL')"
        )
        can_write = bool(write_rows[0][0]) or bool(sysadmin)
        notes = []
        if can_write:
            notes.append(
                "This login can modify data. Stratum executes read-only regardless, "
                "but a dedicated read-only login is safer."
            )
        requested = self.fields.get("option_map", {}).get("profile", "")
        if self._profile is not None:
            profile = self._profile
        elif requested == "auto":
            profile = for_compat_level(int(compat) if compat else None)
        elif requested in PROFILES:
            profile = PROFILES[requested]
        else:
            profile = for_server("mssql", str(major))
        if profile is TSQL2008 and compat and int(compat) >= 110:
            wider = for_compat_level(int(compat))
            notes.append(
                f"This database is at compatibility level {compat}, which allows "
                f"{wider.name} syntax, but the conservative tsql2008 profile is in use. "
                "Add profile=auto to the connection to use the level the database is set to."
            )
        return ServerInfo(
            dialect="mssql",
            version=str(version).splitlines()[0],
            database=database,
            profile=profile,
            privileges=Privileges(
                can_write=can_write, can_ddl=bool(sysadmin), username=user, notes=notes
            ),
        )

    def execute_readonly(
        self, sql: str, *, timeout_ms: int, row_cap: int
    ) -> tuple[list[str], list[tuple]]:
        conn = self._require()
        cur = conn.cursor()
        try:
            # LOCK_TIMEOUT bounds blocking; the query itself is bounded by the driver.
            cur.execute(f"SET LOCK_TIMEOUT {int(timeout_ms)}")
            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(row_cap)
            return columns, [tuple(r) for r in rows]
        finally:
            cur.close()
            conn.rollback()  # the transaction never commits

    def explain(self, sql: str) -> CostEstimate:
        conn = self._require()
        cur = conn.cursor()
        try:
            cur.execute("SET SHOWPLAN_XML ON")
            cur.execute(sql)
            row = cur.fetchone()
            plan_xml = row[0] if row else ""
        except Exception as exc:
            return CostEstimate(supported=False, plan_text=str(exc))
        finally:
            with contextlib.suppress(Exception):
                cur.execute("SET SHOWPLAN_XML OFF")
            cur.close()
            conn.rollback()

        text = str(plan_xml)
        # The estimates live in attributes on the plan root and on each operator.
        rows_match = re.search(r'EstimateRows="([\d.eE+-]+)"', text)
        cost_match = re.search(r'EstimatedTotalSubtreeCost="([\d.eE+-]+)"', text)
        scans = re.findall(r'<TableScan[^>]*?Table="\[?([^"\]]+)', text)
        scans += re.findall(r'Index="\[?([^"\]]+)[^>]*?Lookup="1"', text)
        return CostEstimate(
            estimated_rows=float(rows_match.group(1)) if rows_match else 0.0,
            estimated_cost=float(cost_match.group(1)) if cost_match else 0.0,
            estimated_ms=None,
            scans=scans,
            plan_text=text[:20000],
        )

    def sample_sql_tail(
        self, schema: str, table: str, columns: list[str], limit: int
    ) -> str | None:
        """SQL Server has no cheap physical-order cursor, so order by the declared
        key descending when there is one, and otherwise skip the tail sample."""
        key = self._primary_key_column(schema, table)
        if key is None:
            return None
        cols = ", ".join(self.quote(c) for c in columns) if columns else "*"
        target = self.quote(schema, table) if schema else self.quote(table)
        return f"SELECT TOP {limit} {cols} FROM {target} ORDER BY {self.quote(key)} DESC"

    def _primary_key_column(self, schema: str, table: str) -> str | None:
        for index in self.list_indexes():
            if (
                index.is_primary
                and index.table.lower() == table.lower()
                and index.schema.lower() == schema.lower()
                and index.columns
            ):
                return index.columns[0]
        return None

    # ---------------------------------------------------------------- introspection
    def _query(self, sql: str) -> list[tuple]:
        conn = self._require()
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.rollback()

    def list_tables(self) -> list[TableInfo]:
        rows = self._query(
            """
            SELECT s.name, t.name, 'table',
                   CAST(ISNULL(p.rows, 0) AS BIGINT),
                   CAST(ep.value AS NVARCHAR(4000)), NULL
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            LEFT JOIN sys.partitions p
                   ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            LEFT JOIN sys.extended_properties ep
                   ON ep.major_id = t.object_id AND ep.minor_id = 0
                  AND ep.name = 'MS_Description'
            UNION ALL
            SELECT s.name, v.name, 'view', 0,
                   CAST(ep.value AS NVARCHAR(4000)), m.definition
            FROM sys.views v
            JOIN sys.schemas s ON s.schema_id = v.schema_id
            LEFT JOIN sys.sql_modules m ON m.object_id = v.object_id
            LEFT JOIN sys.extended_properties ep
                   ON ep.major_id = v.object_id AND ep.minor_id = 0
                  AND ep.name = 'MS_Description'
            """
        )
        # A partitioned table produces one row per partition; keep the largest.
        best: dict[tuple[str, str], TableInfo] = {}
        for s, t, kind, n, comment, definition in rows:
            key = (s, t)
            info = TableInfo(schema=s, name=t, kind=kind, row_estimate=int(n or 0),
                             comment=comment, definition=definition)
            if key not in best or info.row_estimate > best[key].row_estimate:
                best[key] = info
        return sorted(best.values(), key=lambda t: (t.schema, t.name))

    def list_columns(self) -> list[ColumnInfo]:
        rows = self._query(
            """
            SELECT s.name, o.name, c.name, ty.name, c.is_nullable, c.column_id,
                   dc.definition, c.is_identity, c.max_length, c.precision, c.scale,
                   CAST(ep.value AS NVARCHAR(4000))
            FROM sys.columns c
            JOIN sys.objects o ON o.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
            LEFT JOIN sys.extended_properties ep
                   ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
                  AND ep.name = 'MS_Description'
            WHERE o.type IN ('U', 'V')
            ORDER BY s.name, o.name, c.column_id
            """
        )
        return [
            ColumnInfo(
                schema=s, table=t, name=n, data_type=str(dt).upper(), nullable=bool(null),
                ordinal=int(pos), default=dflt, is_identity=bool(ident),
                char_length=int(mlen) if mlen and int(mlen) > 0 else None,
                numeric_precision=int(prec) if prec else None,
                numeric_scale=int(scale) if scale is not None else None,
                comment=comment,
            )
            for s, t, n, dt, null, pos, dflt, ident, mlen, prec, scale, comment in rows
        ]

    def list_indexes(self) -> list[IndexInfo]:
        rows = self._query(
            """
            SELECT s.name, t.name, i.name, i.is_unique, i.is_primary_key, c.name, ic.key_ordinal
            FROM sys.indexes i
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.index_columns ic
                 ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
                 ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.type > 0 AND ic.is_included_column = 0
            ORDER BY s.name, t.name, i.name, ic.key_ordinal
            """
        )
        grouped: dict[tuple, IndexInfo] = {}
        for s, t, i, uniq, prim, col, _ord in rows:
            key = (s, t, i)
            if key not in grouped:
                grouped[key] = IndexInfo(s, t, i or f"idx_{t}", [], bool(uniq), bool(prim))
            grouped[key].columns.append(col)
        return list(grouped.values())

    def list_foreign_keys(self) -> list[ForeignKeyInfo]:
        rows = self._query(
            """
            SELECT fk.name, ps.name, pt.name, pc.name, rs.name, rt.name, rc.name,
                   fk.is_disabled, fk.is_not_trusted
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.tables pt ON pt.object_id = fk.parent_object_id
            JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
            JOIN sys.columns pc
                 ON pc.object_id = fkc.parent_object_id
                AND pc.column_id = fkc.parent_column_id
            JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            JOIN sys.columns rc
                 ON rc.object_id = fkc.referenced_object_id
                AND rc.column_id = fkc.referenced_column_id
            ORDER BY fk.name, fkc.constraint_column_id
            """
        )
        grouped: dict[str, ForeignKeyInfo] = {}
        for name, ps, pt, pc, rs, rt, rc, disabled, untrusted in rows:
            if name not in grouped:
                grouped[name] = ForeignKeyInfo(
                    name=name, schema=ps, table=pt, columns=[],
                    ref_schema=rs, ref_table=rt, ref_columns=[],
                    enforced=not (disabled or untrusted),
                    weakness="disabled" if disabled else "not_validated" if untrusted else None,
                )
            grouped[name].columns.append(pc)
            grouped[name].ref_columns.append(rc)
        return list(grouped.values())

    def list_routines(self) -> list[RoutineInfo]:
        rows = self._query(
            """
            SELECT s.name, o.name,
                   CASE o.type WHEN 'P' THEN 'procedure' WHEN 'V' THEN 'view'
                               ELSE 'function' END,
                   m.definition
            FROM sys.sql_modules m
            JOIN sys.objects o ON o.object_id = m.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.type IN ('P', 'V', 'FN', 'IF', 'TF')
            """
        )
        return [
            RoutineInfo(schema=s, name=n, kind=k, definition=d or "")
            for s, n, k, d in rows
        ]


def normalise_type(tsql_type: str) -> str:
    """Collapse T-SQL type spellings onto the handful the detectors care about."""
    t = re.sub(r"\(.*\)", "", tsql_type).strip().lower()
    if t in ("int", "bigint", "smallint", "tinyint"):
        return "int"
    if t in ("decimal", "numeric", "money", "smallmoney", "float", "real"):
        return "decimal"
    if t in ("char", "nchar", "varchar", "nvarchar", "text", "ntext"):
        return "text"
    if t in ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"):
        return "datetime"
    if t == "bit":
        return "bool"
    if t == "xml":
        return "xml"
    if t in ("binary", "varbinary", "image"):
        return "binary"
    if t == "uniqueidentifier":
        return "uuid"
    return t
