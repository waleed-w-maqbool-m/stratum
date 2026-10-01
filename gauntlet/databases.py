"""Where a suite's databases live, whatever engine they run on.

A suite names its databases (`database: pagila_legacy`, `clean: pagila_clean`) and its
dialect; this turns those names into something to connect to. Chinook's are SQLite
files under `build/`, Pagila's are databases on the local Postgres from `gauntlet.pg`.
Everything that needs one — the gold checker, the runner, the knowledge scorer —
resolves it here, so none of them assumes SQLite.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"

SQL = Callable[[str], list[tuple]]


def is_postgres(dialect: str) -> bool:
    return dialect.lower() in ("postgres", "postgresql", "pg16")


def legacy_url(database: str, dialect: str) -> str:
    """The URL the engine's connector registry is given for the system under test."""
    if is_postgres(dialect):
        from gauntlet import pg

        return pg.sqlalchemy_url(database)
    return f"sqlite:///{BUILD / f'{database}.sqlite'}"


def manifest_path(database: str) -> Path:
    return BUILD / f"{database}.manifest.json"


def clean_runner(clean: str, dialect: str) -> SQL:
    """A function that runs gold SQL against the clean copy, read-only.

    Postgres runs in autocommit, so one failing gold query cannot leave the connection
    in an aborted transaction and fail every query after it.
    """
    if is_postgres(dialect):
        import psycopg

        from gauntlet import pg

        state: dict[str, psycopg.Connection] = {}

        def connection() -> psycopg.Connection:
            conn = state.get("conn")
            if conn is None or conn.closed or conn.broken:
                conn = psycopg.connect(pg.uri(clean), autocommit=True)
                conn.execute("SET default_transaction_read_only = on")
                state["conn"] = conn
            return conn

        def run_pg(sql: str) -> list[tuple]:
            # Reconnect once if the session was lost: an eval holds this for an hour.
            try:
                return [tuple(row) for row in connection().execute(sql)]
            except psycopg.OperationalError:
                if not (state["conn"].closed or state["conn"].broken):
                    raise
                return [tuple(row) for row in connection().execute(sql)]

        return run_pg

    path = BUILD / f"{clean}.sqlite"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def run_sqlite(sql: str) -> list[tuple]:
        return [tuple(row) for row in conn.execute(sql)]

    return run_sqlite


def clean_query(clean: str, dialect: str):
    """Like `clean_runner`, but returns (column names, rows) — for showing gold answers
    to a person, where a column called `revenue` reads better than `col2`."""
    if is_postgres(dialect):
        import psycopg

        from gauntlet import pg

        conn = psycopg.connect(pg.uri(clean), autocommit=True)
        conn.execute("SET default_transaction_read_only = on")

        def run_pg(sql: str) -> tuple[list[str], list[tuple]]:
            cursor = conn.execute(sql)
            return [d.name for d in cursor.description or []], [tuple(r) for r in cursor]

        return run_pg

    conn = sqlite3.connect(f"file:{BUILD / f'{clean}.sqlite'}?mode=ro", uri=True)

    def run_sqlite(sql: str) -> tuple[list[str], list[tuple]]:
        cursor = conn.execute(sql)
        return [d[0] for d in cursor.description or []], [tuple(r) for r in cursor]

    return run_sqlite


def main() -> int:
    """`python -m gauntlet.databases url pagila_v1` — for the Makefile."""
    import argparse
    import sys

    from gauntlet.scorer import Suite

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("what", choices=["url", "name"])
    parser.add_argument("suite")
    args = parser.parse_args()
    suite = Suite.load(HERE / "suites" / f"{args.suite}.yaml")
    if args.what == "url":
        # Relative SQLite paths are resolved from the repo root; the engine CLI runs from
        # engine/, so hand it an absolute one.
        print(legacy_url(suite.database, suite.dialect))
    else:
        print(suite.database)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
