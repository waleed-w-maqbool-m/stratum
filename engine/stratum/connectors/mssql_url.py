"""Connecting to SQL Server the way SSMS does.

SSMS, Visual Studio and every .NET config file describe a connection as an ADO.NET string:
`Server=.\\SQLEXPRESS;Database=Northwind;Trusted_Connection=True;`. That is what a person
has in hand, so it is accepted as is and turned into the URL the rest of Stratum uses.

A named instance (`.\\SQLEXPRESS`) and Windows authentication are the two things SSMS users
rely on that a plain host-and-port login cannot express. Both go through ODBC; a login with
a username and password on a known port keeps using pymssql, which needs no driver install.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlparse

_KEYS = {
    "server": "server", "data source": "server", "address": "server", "addr": "server",
    "network address": "server",
    "database": "database", "initial catalog": "database",
    "user id": "user", "uid": "user", "user": "user",
    "password": "password", "pwd": "password",
    "trusted_connection": "windows", "integrated security": "windows",
    "encrypt": "encrypt", "trustservercertificate": "trust_cert",
}

_TRUE = {"true", "yes", "sspi", "1"}
_LOCAL = {".", "(local)", "localhost", "127.0.0.1", "::1"}

# Newest first. The oldest entries are the drivers that ship with Windows itself, which
# is why they are still worth trying: they need nothing installed.
_DRIVERS = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13.1 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server Native Client 10.0",
    "SQL Server",
]


def looks_like_ado(text: str) -> bool:
    lowered = text.strip().lower()
    return "://" not in lowered and "=" in lowered and (
        ";" in lowered or lowered.startswith(("server=", "data source="))
    )


def _pairs(text: str) -> list[tuple[str, str]]:
    """Split `key=value;key=value`, where a value may be quoted to hold a semicolon."""
    out: list[tuple[str, str]] = []
    key, value, quote_char, in_value = "", "", "", False
    for char in text:
        if quote_char:
            if char == quote_char:
                quote_char = ""
            else:
                value += char
        elif char in "'\"" and in_value and not value:
            quote_char = char
        elif char == "=" and not in_value:
            in_value = True
        elif char == ";":
            if key.strip():
                out.append((key.strip(), value.strip()))
            key, value, in_value = "", "", False
        elif in_value:
            value += char
        else:
            key += char
    if key.strip():
        out.append((key.strip(), value.strip()))
    return out


def _split_server(value: str) -> tuple[str, str, int | None]:
    value = value.strip()
    if value.lower().startswith("tcp:"):
        value = value[4:]
    port: int | None = None
    if "," in value:
        value, _, tail = value.partition(",")
        port = int(tail) if tail.strip().isdigit() else None
    instance = ""
    if "\\" in value:
        value, _, instance = value.partition("\\")
    host = "localhost" if value.strip().lower() in _LOCAL else value.strip()
    return host, instance, port


def parse_ado(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "host": "", "instance": "", "port": None, "database": "", "username": "",
        "password": "", "windows_auth": False, "encrypt": None, "trust_cert": None,
    }
    for raw_key, value in _pairs(text):
        key = _KEYS.get(raw_key.lower())
        if key == "server":
            fields["host"], fields["instance"], fields["port"] = _split_server(value)
        elif key == "database":
            fields["database"] = value
        elif key == "user":
            fields["username"] = value
        elif key == "password":
            fields["password"] = value
        elif key == "windows":
            fields["windows_auth"] = value.lower() in _TRUE
        elif key in ("encrypt", "trust_cert"):
            fields[key] = value.lower() in _TRUE
    return fields


def to_url(fields: dict[str, Any]) -> str:
    """The `mssql://` URL for parsed fields. Instance and options live in the query."""
    auth = ""
    if fields.get("username"):
        auth = quote(fields["username"], safe="")
        if fields.get("password"):
            auth += ":" + quote(fields["password"], safe="")
        auth += "@"
    host = fields.get("host") or "localhost"
    port = f":{fields['port']}" if fields.get("port") else ""
    query = []
    if fields.get("instance"):
        query.append(f"instance={quote(fields['instance'], safe='')}")
    if fields.get("windows_auth"):
        query.append("auth=windows")
    for name in ("encrypt", "trust_cert"):
        if fields.get(name) is not None:
            query.append(f"{name}={'1' if fields[name] else '0'}")
    tail = ("?" + "&".join(query)) if query else ""
    return f"mssql://{auth}{host}{port}/{quote(fields.get('database') or '', safe='')}{tail}"


def normalise(text: str) -> str:
    """A URL for either input: an ADO.NET string is converted, a URL passes through."""
    return to_url(parse_ado(text)) if looks_like_ado(text) else text


def fields_from_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    options = dict(parse_qsl(parsed.query))
    host = parsed.hostname or ""
    return {
        "host": "localhost" if host.lower() in _LOCAL else host,
        "instance": options.get("instance", ""),
        "port": parsed.port,
        "database": unquote(parsed.path.lstrip("/")),
        "username": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
        "windows_auth": options.get("auth", "").lower() == "windows",
        "encrypt": None if "encrypt" not in options else options["encrypt"] in _TRUE,
        "trust_cert": None if "trust_cert" not in options else options["trust_cert"] in _TRUE,
        "options": options,
    }


def needs_odbc(fields: dict[str, Any]) -> bool:
    """Windows authentication and named instances are what pymssql cannot do."""
    return bool(
        fields.get("windows_auth") or fields.get("instance")
        or (fields.get("option_map") or {}).get("driver") == "odbc"
    )


def pick_driver(available: list[str]) -> str | None:
    wanted = {name.lower(): name for name in available}
    for candidate in _DRIVERS:
        if candidate.lower() in wanted:
            return wanted[candidate.lower()]
    return None


def _escape(value: str) -> str:
    if any(c in value for c in ";{}="):
        return "{" + value.replace("}", "}}") + "}"
    return value


def odbc_connection_string(fields: dict[str, Any], driver: str) -> str:
    server = fields["host"] or "localhost"
    if fields.get("instance"):
        server += "\\" + fields["instance"]
    if fields.get("port"):
        server += f",{fields['port']}"
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}"]
    if fields.get("database"):
        parts.append(f"DATABASE={_escape(fields['database'])}")
    if fields.get("windows_auth"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={_escape(fields.get('username', ''))}")
        parts.append(f"PWD={_escape(fields.get('password', ''))}")

    # The legacy drivers know nothing about encryption keywords. The modern ones encrypt
    # by default, and a local Express instance has a self-signed certificate, so on this
    # machine alone the certificate is trusted unless the caller said otherwise.
    if "ODBC Driver" in driver:
        local = (fields.get("host") or "localhost").lower() in _LOCAL
        trust = fields.get("trust_cert")
        if trust is None and local:
            trust = True
        if fields.get("encrypt") is not None:
            parts.append(f"Encrypt={'yes' if fields['encrypt'] else 'no'}")
        if trust is not None:
            parts.append(f"TrustServerCertificate={'yes' if trust else 'no'}")
    parts.append("ApplicationName=Stratum")
    return ";".join(parts)
