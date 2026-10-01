"""Download the clean source databases the gauntlet is built from.

    python -m gauntlet.sources.fetch chinook
    python -m gauntlet.sources.fetch --all

Sources are never modified in place; the legacifiers copy them into `gauntlet/build/`
before touching anything.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

PAGILA_TAG = "pagila-v3.1.0"

SOURCES: dict[str, dict[str, str]] = {
    "chinook": {
        "url": (
            "https://github.com/lerocha/chinook-database/raw/master/"
            "ChinookDatabase/DataSources/Chinook_Sqlite.sqlite"
        ),
        "filename": "chinook_clean.sqlite",
        "note": "Chinook, the SQLite media-store sample.",
    },
    # Pinned to a tag, not master. Upstream master now targets Postgres 18 (uuidv7(),
    # VIRTUAL generated columns, transaction_timeout) and fails to load on 16; a moving
    # source is also a moving benchmark. v3.1.0 is the last release that loads cleanly
    # on the pg16 capability profile.
    "pagila": {
        "url": f"https://github.com/devrimgunduz/pagila/raw/{PAGILA_TAG}/pagila-schema.sql",
        "filename": "pagila-schema.sql",
        "note": "Pagila schema; the data file is fetched alongside it.",
    },
    "pagila-data": {
        "url": f"https://github.com/devrimgunduz/pagila/raw/{PAGILA_TAG}/pagila-data.sql",
        "filename": "pagila-data.sql",
        "note": "Pagila data, loaded into Postgres by gauntlet/pg.py.",
    },
}

# AdventureWorks is a SQL Server .bak restore rather than a plain download; see
# infra/README.md. It is fetched by infra/restore_adventureworks.ps1 because the
# restore has to happen inside the container.
ADVENTUREWORKS_URL = (
    "https://github.com/Microsoft/sql-server-samples/releases/download/"
    "adventureworks/AdventureWorks2019.bak"
)


def fetch(name: str, force: bool = False) -> Path:
    spec = SOURCES[name]
    target = HERE / spec["filename"]
    if target.exists() and not force:
        print(f"{name}: already present at {target} ({target.stat().st_size / 1e6:.1f} MB)")
        return target
    print(f"{name}: downloading {spec['url']}")
    tmp = target.with_suffix(target.suffix + ".part")
    urllib.request.urlretrieve(spec["url"], tmp)  # noqa: S310 - fixed, vetted URLs
    tmp.replace(target)
    print(f"{name}: wrote {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", choices=[*SOURCES, []], help="sources to fetch")
    parser.add_argument("--all", action="store_true", help="fetch every source")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()

    names = list(SOURCES) if args.all else args.names
    if not names:
        parser.print_help()
        print("\nAdventureWorks is restored inside the SQL Server container; see infra/README.md")
        return 1
    for name in names:
        fetch(name, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
