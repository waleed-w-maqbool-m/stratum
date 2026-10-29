"""Connecting to SQL Server the way SSMS does, without needing a SQL Server.

What a person has in hand is an ADO.NET string, a named instance and Windows
authentication. These pin the parsing, the choice of driver, the connection string handed
to ODBC and the SQL profile chosen from the database's compatibility level. Nothing here
connects to anything; the live test at the bottom skips unless a server is reachable.
"""

from __future__ import annotations

import os

import pytest

from stratum.ask import dialect as dialect_module
from stratum.connectors import mssql_url
from stratum.connectors.base import parse_url
from stratum.connectors.profiles import (
    TSQL2008,
    TSQL2012,
    TSQL2016,
    TSQL2017,
    for_compat_level,
)
from stratum.connectors.registry import build


def test_the_string_ssms_shows_for_a_local_express_instance():
    fields = mssql_url.parse_ado(r"Server=.\SQLEXPRESS;Database=Northwind;Trusted_Connection=True;")
    assert fields["host"] == "localhost" and fields["instance"] == "SQLEXPRESS"
    assert fields["database"] == "Northwind" and fields["windows_auth"] is True
    assert fields["port"] is None


@pytest.mark.parametrize("server, host, instance, port", [
    ("localhost,1433", "localhost", "", 1433),
    ("tcp:db.example.com,1444", "db.example.com", "", 1444),
    (r"(local)\SQLEXPRESS", "localhost", "SQLEXPRESS", None),
    (r"PROD01\REPORTING", "PROD01", "REPORTING", None),
    ("10.0.0.5", "10.0.0.5", "", None),
])
def test_every_way_of_writing_a_server_name(server, host, instance, port):
    fields = mssql_url.parse_ado(f"Data Source={server};Initial Catalog=x;")
    assert (fields["host"], fields["instance"], fields["port"]) == (host, instance, port)


def test_sql_login_aliases_and_quoted_values():
    fields = mssql_url.parse_ado(
        "Address=srv;Initial Catalog=Sales;UID=reader;PWD='p;ss=word';Encrypt=False;"
        "TrustServerCertificate=True"
    )
    assert fields["username"] == "reader" and fields["password"] == "p;ss=word"
    assert fields["windows_auth"] is False
    assert fields["encrypt"] is False and fields["trust_cert"] is True
    assert mssql_url.parse_ado("Integrated Security=SSPI;Server=a")["windows_auth"] is True


def test_an_ado_string_is_told_apart_from_a_url():
    assert mssql_url.looks_like_ado(r"Server=.\SQLEXPRESS;Database=x")
    assert mssql_url.looks_like_ado("Server=localhost")
    assert not mssql_url.looks_like_ado("mssql://user:pw@host:1433/db?x=1")
    assert not mssql_url.looks_like_ado("postgresql://u:p@h/db")
    assert not mssql_url.looks_like_ado("sqlite:///a=b.db")


def test_the_url_round_trips_instance_auth_and_special_characters():
    original = mssql_url.parse_ado(
        r"Server=.\SQLEXPRESS;Database=My DB;Trusted_Connection=yes;TrustServerCertificate=yes"
    )
    again = parse_url(mssql_url.to_url(original))
    assert again["host"] == "localhost" and again["instance"] == "SQLEXPRESS"
    assert again["database"] == "My DB" and again["windows_auth"] is True
    assert again["trust_cert"] is True
    assert again["port"] is None            # a named instance is found by name, not by 1433

    login = parse_url(mssql_url.to_url(mssql_url.parse_ado(
        "Server=h,1500;Database=d;User Id=a@b;Password=p@ss/w:rd")))
    assert (login["username"], login["password"], login["port"]) == ("a@b", "p@ss/w:rd", 1500)


def test_a_plain_url_keeps_its_default_port():
    assert parse_url("mssql://u:p@host/db")["port"] == 1433


def test_the_registry_accepts_an_ado_string_directly():
    connector = build(r"Server=.\SQLEXPRESS;Database=Northwind;Trusted_Connection=True;")
    assert connector.dialect == "mssql"
    assert connector.fields["instance"] == "SQLEXPRESS"
    assert mssql_url.needs_odbc(connector.fields)


def test_only_windows_auth_and_named_instances_need_odbc():
    assert not mssql_url.needs_odbc(parse_url("mssql://u:p@host:1433/db"))
    assert mssql_url.needs_odbc(parse_url("mssql://host/db?auth=windows"))
    assert mssql_url.needs_odbc(parse_url("mssql://host/db?instance=SQLEXPRESS"))


def test_the_newest_installed_driver_is_chosen_and_none_is_reported():
    assert mssql_url.pick_driver(
        ["SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]
    ) == "ODBC Driver 18 for SQL Server"
    assert mssql_url.pick_driver(["SQL Server", "PostgreSQL Unicode"]) == "SQL Server"
    assert mssql_url.pick_driver(["PostgreSQL Unicode"]) is None


def test_the_odbc_string_for_a_local_express_instance_trusts_its_own_certificate():
    fields = parse_url(r"mssql://localhost/Northwind?instance=SQLEXPRESS&auth=windows")
    text = mssql_url.odbc_connection_string(fields, "ODBC Driver 18 for SQL Server")
    assert r"SERVER=localhost\SQLEXPRESS" in text
    assert "Trusted_Connection=yes" in text and "UID=" not in text
    assert "TrustServerCertificate=yes" in text


def test_a_remote_server_is_not_given_certificate_trust_it_did_not_ask_for():
    fields = parse_url("mssql://reader:pw@db.example.com:1433/Sales")
    text = mssql_url.odbc_connection_string(fields, "ODBC Driver 18 for SQL Server")
    assert "SERVER=db.example.com,1433" in text and "UID=reader" in text
    assert "TrustServerCertificate" not in text
    explicit = parse_url("mssql://reader:pw@db.example.com/Sales?trust_cert=1&encrypt=1")
    assert "TrustServerCertificate=yes" in mssql_url.odbc_connection_string(
        explicit, "ODBC Driver 17 for SQL Server")


def test_the_legacy_windows_driver_is_not_sent_keywords_it_does_not_know():
    fields = parse_url("mssql://localhost/db?auth=windows")
    text = mssql_url.odbc_connection_string(fields, "SQL Server")
    assert "TrustServerCertificate" not in text and "Encrypt" not in text


def test_a_password_with_odbc_metacharacters_is_escaped():
    fields = parse_url("mssql://u:" + "a%3Bb%7Dc" + "@h/db")        # a;b}c
    assert "PWD={a;b}}c}" in mssql_url.odbc_connection_string(fields, "ODBC Driver 18 for SQL Server")


# ------------------------------------------------------------------------ profiles


@pytest.mark.parametrize("level, profile", [
    (None, TSQL2008), (90, TSQL2008), (100, TSQL2008), (110, TSQL2012), (120, TSQL2012),
    (130, TSQL2016), (140, TSQL2017), (150, TSQL2017), (160, TSQL2017),
])
def test_the_profile_follows_the_databases_compatibility_level(level, profile):
    assert for_compat_level(level) is profile


def test_each_profile_allows_exactly_what_its_version_added():
    paging = "SELECT id FROM t ORDER BY id OFFSET 5 ROWS FETCH NEXT 5 ROWS ONLY"
    agg = "SELECT STRING_AGG(name, ',') AS g FROM t"
    assert not dialect_module.lint(paging, TSQL2008).ok
    assert dialect_module.lint(paging, TSQL2012).ok
    assert not dialect_module.lint(agg, TSQL2012).ok
    assert not dialect_module.lint(agg, TSQL2016).ok          # STRING_AGG is 2017, not 2016
    assert dialect_module.lint(agg, TSQL2017).ok


# ---------------------------------------------------------------------- live (opt in)

LIVE = os.environ.get("STRATUM_MSSQL_URL", "")


@pytest.mark.skipif(not LIVE, reason="set STRATUM_MSSQL_URL to run against a real SQL Server")
def test_live_server_connects_and_reports_a_profile():
    from stratum.connectors.registry import test_connection

    result = test_connection(LIVE)
    assert result.ok, result.error_hint or result.error
    assert result.dialect == "mssql" and result.table_count >= 0
