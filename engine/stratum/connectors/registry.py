"""Connector construction from a URL, plus connection testing.

`POST /connections/test` calls `test_connection`; the Connect page shows whatever it
returns, including the friendly error hint when it fails.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
from typing import Any

from .base import Connector, parse_url
from .profiles import CapabilityProfile, get


def build(url: str, profile_name: str | None = None) -> Connector:
    from .mssql_url import looks_like_ado, normalise

    if looks_like_ado(url):
        url = normalise(url)             # what SSMS shows: Server=.\SQLEXPRESS;Database=...
    fields = parse_url(url)
    dialect = fields["dialect"]
    profile: CapabilityProfile | None = get(profile_name) if profile_name else None

    if dialect == "sqlite":
        from .sqlite import SQLiteConnector

        return SQLiteConnector(url, profile)
    if dialect == "postgres":
        from .postgres import PostgresConnector

        return PostgresConnector(url, profile)
    if dialect == "mssql":
        from .mssql import MSSQLConnector

        return MSSQLConnector(url, profile)
    raise ValueError(
        f"unsupported database URL scheme {dialect!r}; "
        "expected postgresql://, mssql:// or sqlite:///"
    )


@dataclass
class ConnectionTest:
    ok: bool
    dialect: str = ""
    version: str = ""
    database: str = ""
    profile: str = ""
    can_write: bool = False
    username: str = ""
    notes: list[str] | None = None
    table_count: int = 0
    error: str = ""
    error_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def test_connection(url: str, profile_name: str | None = None) -> ConnectionTest:
    connector: Connector | None = None
    try:
        connector = build(url, profile_name)
        connector.connect()
        info = connector.server_info()
        tables = connector.list_tables()
        return ConnectionTest(
            ok=True,
            dialect=info.dialect,
            version=info.version,
            database=info.database,
            profile=info.profile.name,
            can_write=info.privileges.can_write,
            username=info.privileges.username,
            notes=info.privileges.notes,
            table_count=len(tables),
        )
    except Exception as exc:
        hint = connector.friendly_error(exc) if connector else str(exc)
        return ConnectionTest(ok=False, error=str(exc), error_hint=hint)
    finally:
        if connector is not None:
            with contextlib.suppress(Exception):
                connector.close()


def describe_url(url: str) -> dict[str, Any]:
    """What the Connect page echoes back so the user can see what was understood.

    The password is never echoed, only whether one was present.
    """
    fields = parse_url(url)
    redacted = dict(fields)
    if redacted.get("password"):
        redacted["password"] = "•" * 8
        redacted["has_password"] = True
    else:
        redacted["has_password"] = False
    return redacted
