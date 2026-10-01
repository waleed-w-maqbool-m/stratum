"""A local PostgreSQL 16 for the gauntlet, without Docker.

The compose file in `infra/` is still the intended way to run the benchmark servers, but
it needs hardware virtualisation, and not every machine has it switched on. `pgserver`
ships real Postgres binaries as a wheel and runs them as an ordinary user process, so the
pg16 half of the gauntlet runs anywhere the engine's virtualenv does.

The server lives under `gauntlet/build/pg/` and outlives the Python process that started
it; asking for it again reattaches rather than starting a second one.

    python -m gauntlet.pg load-clean     # create pagila_clean from gauntlet/sources/
    python -m gauntlet.pg uri pagila_legacy
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "build" / "pg" / "data"
SOURCES = HERE / "sources"


def server():
    """The running server, starting it if it is not up."""
    import pgserver

    DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    # cleanup_mode=None keeps the server running after this process exits, so the
    # harness and the engine can both reach it without racing to start it.
    return pgserver.get_server(str(DATA_DIR), cleanup_mode=None)


def uri(database: str = "postgres") -> str:
    base = server().get_uri()
    return base.rsplit("/", 1)[0] + f"/{database}"


def sqlalchemy_url(database: str) -> str:
    """The same database as a URL the engine's connector registry accepts.

    The registry takes a plain `postgresql://` URL and chooses the driver itself, so
    this is the libpq URI unchanged; it exists so callers do not need to know that.
    """
    return uri(database)


def recreate(database: str) -> str:
    """Drop and create a database — but never out from under a live session.

    This used `DROP DATABASE ... WITH (FORCE)`, which terminates every connection to
    it. Rebuilding the gauntlet while an eval was running killed the eval mid-question.
    A benchmark database in use is refused with a message saying who is using it.
    """
    import psycopg

    with psycopg.connect(uri(), autocommit=True) as conn:
        in_use = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        ).fetchone()[0]
        if in_use:
            raise SystemExit(
                f"{database} has {in_use} open connection(s) — probably an eval run. "
                "Let it finish (or stop it) before rebuilding the gauntlet."
            )
        conn.execute(f'DROP DATABASE IF EXISTS "{database}"')
        conn.execute(f'CREATE DATABASE "{database}"')
    return uri(database)


def psql_binary() -> Path:
    import pgserver

    return Path(pgserver.__file__).resolve().parent / "pginstall" / "bin" / "psql.exe" \
        if sys.platform == "win32" else \
        Path(pgserver.__file__).resolve().parent / "pginstall" / "bin" / "psql"


def run_file(database: str, sql_file: Path) -> list[str]:
    """Run a SQL script with psql and return the error lines.

    psql, not a driver: pg_dump output loads its data with `COPY ... FROM stdin`, which
    only psql knows how to feed. Options go before the database argument — psql on
    Windows stops reading options at the first positional one.
    """
    result = subprocess.run(
        [str(psql_binary()), "-q", "-X", "-f", str(sql_file), uri(database)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return [line for line in result.stderr.splitlines() if "ERROR" in line]


def load_clean(database: str = "pagila_clean") -> str:
    """Build the clean pagila database from the pinned source dump."""
    schema = SOURCES / "pagila-schema.sql"
    data = SOURCES / "pagila-data.sql"
    missing = [p.name for p in (schema, data) if not p.exists()]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}; "
            "run: python -m gauntlet.sources.fetch pagila pagila-data"
        )
    recreate(database)
    errors = run_file(database, schema) + run_file(database, data)
    if errors:
        raise SystemExit("pagila failed to load:\n  " + "\n  ".join(errors[:10]))
    return uri(database)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("load-clean", help="create pagila_clean from gauntlet/sources")
    p_uri = sub.add_parser("uri", help="print a database's connection URI")
    p_uri.add_argument("database", nargs="?", default="postgres")
    p_url = sub.add_parser("url", help="print a database's SQLAlchemy URL for the engine")
    p_url.add_argument("database")
    args = parser.parse_args()

    if args.command == "load-clean":
        print(load_clean())
    elif args.command == "uri":
        print(uri(args.database))
    else:
        print(sqlalchemy_url(args.database))
    return 0


if __name__ == "__main__":
    sys.exit(main())
