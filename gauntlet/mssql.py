"""The SQL Server the HALCYON benchmark runs on.

`make up` starts SQL Server 2022 in Docker (infra/docker-compose.yml) with
`gauntlet/sources` mounted at `/sources`, so the AdventureWorks backup can be restored
from inside the container. This module is the gauntlet's only way in: restoring the
clean source, recreating the legacy database, and handing out connection URLs.

Part of the gauntlet, not the engine. The engine reaches the same server through its
own connector and the URL `sqlalchemy_url` returns.
"""

from __future__ import annotations

import os
import time
from typing import Any

HOST = os.environ.get("MSSQL_HOST", "localhost")
PORT = int(os.environ.get("MSSQL_PORT", "1433"))
USER = "sa"
PASSWORD = os.environ.get("MSSQL_SA_PASSWORD", "Stratum!Passw0rd")

# Inside the container; see the volume mounts in infra/docker-compose.yml.
BACKUP = "/sources/AdventureWorks2019.bak"
DATA_DIR = "/var/opt/mssql/data"


def connect(database: str = "master", *, autocommit: bool = True, timeout: int = 600) -> Any:
    import pymssql

    return pymssql.connect(
        server=HOST, port=str(PORT), user=USER, password=PASSWORD, database=database,
        autocommit=autocommit, timeout=timeout, login_timeout=15,
    )


def wait_until_ready(seconds: float = 180.0) -> None:
    """SQL Server takes a while to accept logins after the container starts."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            connect().close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(3)


def sqlalchemy_url(database: str) -> str:
    """The URL the engine's connector registry is given."""
    from urllib.parse import quote

    return f"mssql://{USER}:{quote(PASSWORD, safe='')}@{HOST}:{PORT}/{database}"


def exists(database: str) -> bool:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.databases WHERE name = %s", (database,))
        return bool(cur.fetchone()[0])
    finally:
        conn.close()


def _in_use(conn: Any, database: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM sys.dm_exec_sessions "
        "WHERE database_id = DB_ID(%s) AND session_id <> @@SPID",
        (database,),
    )
    return int(cur.fetchone()[0])


def drop(database: str) -> None:
    """Drop a database, refusing if anything is connected to it.

    Kicking sessions out would make a rebuild silently break a running engine or eval;
    better to say so and let the person stop it.
    """
    conn = connect()
    try:
        if not exists(database):
            return
        sessions = _in_use(conn, database)
        if sessions:
            raise RuntimeError(
                f"{database} has {sessions} open connection(s); stop the engine or eval "
                "using it before rebuilding."
            )
        conn.cursor().execute(f"DROP DATABASE [{database}]")
    finally:
        conn.close()


def restore_clean(name: str = "AW_CLEAN") -> None:
    """Restore AdventureWorks under `name`, replacing any earlier copy."""
    drop(name)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"RESTORE FILELISTONLY FROM DISK = N'{BACKUP}'")
        files = cur.fetchall()
        moves = ", ".join(
            f"MOVE N'{logical}' TO N'{DATA_DIR}/{name}_{index}"
            f"{'.ldf' if kind == 'L' else '.mdf'}'"
            for index, (logical, _physical, kind, *_rest) in enumerate(files)
        )
        cur.execute(f"RESTORE DATABASE [{name}] FROM DISK = N'{BACKUP}' WITH {moves}, REPLACE")
        while cur.nextset():  # RESTORE reports progress as result sets
            pass
    finally:
        conn.close()


def recreate(database: str, *, compatibility: int | None = None,
             collation: str | None = None) -> None:
    drop(database)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE DATABASE [{database}]" + (f" COLLATE {collation}" if collation else "")
        )
        cur.execute(f"ALTER DATABASE [{database}] SET RECOVERY SIMPLE")
        if compatibility:
            cur.execute(f"ALTER DATABASE [{database}] SET COMPATIBILITY_LEVEL = {compatibility}")
    finally:
        conn.close()
