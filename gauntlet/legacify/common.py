"""Shared machinery for the legacifiers.

A legacifier reads a *clean* source database and deterministically writes a *legacy*
copy with documented anti-patterns injected into it, plus a `manifest.json` recording
every transformation.

The manifest is the answer key for the knowledge-recall score. Engine code must never
read it; only `gauntlet/scorer.py` may.

Everything here is seeded and order-stable: running a legacifier twice on the same
source produces the same output.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

SEED = 20240312


# --------------------------------------------------------------------------- manifest


@dataclass
class Manifest:
    """The answer key.

    Each `record_*` call appends a typed entry describing a ground truth that the
    excavation pipeline is supposed to rediscover on its own.
    """

    database: str
    dialect: str
    source: str
    scale_notes: dict[str, str] = field(default_factory=dict)

    renames: dict[str, str] = field(default_factory=dict)          # physical -> logical entity
    column_renames: dict[str, str] = field(default_factory=dict)   # "TBL.COL" -> "entity.column"
    joins: list[dict[str, Any]] = field(default_factory=list)
    keys: dict[str, list[str]] = field(default_factory=dict)
    enums: dict[str, dict[str, str]] = field(default_factory=dict)
    soft_deletes: dict[str, dict[str, Any]] = field(default_factory=dict)
    table_classes: dict[str, str] = field(default_factory=dict)    # core/archive/staging/backup/dead
    drift: list[dict[str, Any]] = field(default_factory=list)
    embedded_formats: list[dict[str, Any]] = field(default_factory=list)
    overloaded: list[dict[str, Any]] = field(default_factory=list)
    god_tables: list[dict[str, Any]] = field(default_factory=list)
    type_mismatches: list[dict[str, Any]] = field(default_factory=list)
    quality: list[dict[str, Any]] = field(default_factory=list)
    perf_traps: list[dict[str, Any]] = field(default_factory=list)

    def rename(self, physical: str, logical: str) -> None:
        self.renames[physical] = logical

    def col(self, physical_table: str, physical_col: str, entity: str, logical: str) -> None:
        self.column_renames[f"{physical_table}.{physical_col}"] = f"{entity}.{logical}"

    def join(
        self,
        *,
        child: str,
        child_cols: Sequence[str],
        parent: str,
        parent_cols: Sequence[str],
        note: str = "",
        cast: str = "",
        condition: str = "",
    ) -> None:
        self.joins.append(
            {
                "child": child,
                "child_cols": list(child_cols),
                "parent": parent,
                "parent_cols": list(parent_cols),
                "note": note,
                "cast": cast,
                "condition": condition,
            }
        )

    def key(self, table: str, cols: Sequence[str]) -> None:
        self.keys[table] = list(cols)

    def enum(self, table: str, column: str, mapping: dict[Any, str]) -> None:
        self.enums[f"{table}.{column}"] = {str(k): v for k, v in mapping.items()}

    def soft_delete(
        self,
        table: str,
        *,
        style: str,
        predicate: str,
        live_predicate: str,
        injected_rows: int = 0,
    ) -> None:
        self.soft_deletes[table] = {
            "style": style,
            "deleted_predicate": predicate,
            "live_predicate": live_predicate,
            "injected_rows": injected_rows,
        }

    def classify(self, table: str, klass: str) -> None:
        self.table_classes[table] = klass

    def record_drift(self, **kw: Any) -> None:
        self.drift.append(kw)

    def record_format(self, **kw: Any) -> None:
        self.embedded_formats.append(kw)

    def record_overload(self, **kw: Any) -> None:
        self.overloaded.append(kw)

    def record_god_table(self, **kw: Any) -> None:
        self.god_tables.append(kw)

    def record_type_mismatch(self, **kw: Any) -> None:
        self.type_mismatches.append(kw)

    def record_quality(self, **kw: Any) -> None:
        self.quality.append(kw)

    def record_perf_trap(self, **kw: Any) -> None:
        self.perf_traps.append(kw)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2, sort_keys=True, default=str), "utf-8")


# ------------------------------------------------------------------------ name mangling

_VOWELS = re.compile(r"[aeiou]")


def drop_vowels(word: str) -> str:
    """`Customer` -> `CSTMR`. Keeps the leading character even if it is a vowel."""
    if not word:
        return word
    return (word[0] + _VOWELS.sub("", word[1:])).upper()


def mainframe(word: str, width: int = 8) -> str:
    """Six-to-eight character mainframe style: vowel-dropped, then truncated."""
    return drop_vowels(word)[:width]


# ------------------------------------------------------------------------ value mangling

NULL_MARKERS = ["", "N/A", "-", "UNKNOWN", "   "]

COUNTRY_VARIANTS = {
    "USA": ["USA", "US", "United States", "U.S.A.", "usa"],
    "Canada": ["Canada", "CA", "CAN", "canada"],
    "United Kingdom": ["United Kingdom", "UK", "GB", "U.K."],
    "Germany": ["Germany", "DE", "GER", "Deutschland"],
    "France": ["France", "FR", "FRA"],
    "Brazil": ["Brazil", "BR", "BRA", "brasil"],
}


def pad_char(value: str | None, width: int) -> str | None:
    """CHAR(n) semantics: right-pad with spaces. The inverse is RTRIM."""
    if value is None:
        return None
    return value[:width].ljust(width)


def zero_pad(value: int | None, width: int = 10) -> str | None:
    """`12345` -> `'0000012345'`. Inverse is a numeric cast, or a padded string compare."""
    if value is None:
        return None
    return str(value).rjust(width, "0")


def yyyymmdd(value: Any) -> str | None:
    d = as_date(value)
    return d.strftime("%Y%m%d") if d else None


def ddmmyyyy(value: Any) -> str | None:
    d = as_date(value)
    return d.strftime("%d/%m/%Y") if d else None


def iso_datetime(value: Any) -> str | None:
    d = as_datetime(value)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def as_date(value: Any) -> date | None:
    dt = as_datetime(value)
    return dt.date() if dt else None


def to_cents(amount: float | None) -> int | None:
    """Unit drift: money stored as an integer number of cents."""
    if amount is None:
        return None
    return int(round(float(amount) * 100))


def kv_pack(pairs: dict[str, Any], sep: str = "|", eq: str = "=") -> str:
    """`{"color": "Red", "size": "M"}` -> `color=Red|size=M`."""
    return sep.join(f"{k}{eq}{v}" for k, v in pairs.items() if v is not None and v != "")


def xml_pack(root: str, fields: dict[str, Any]) -> str:
    body = "".join(
        f"<{k}>{xml_escape(v)}</{k}>" for k, v in fields.items() if v is not None and v != ""
    )
    return f"<{root}>{body}</{root}>"


def xml_escape(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def json_pack(fields: dict[str, Any]) -> str:
    return json.dumps(fields, separators=(",", ":"), sort_keys=True)


# ------------------------------------------------------------------------ sqlite helpers


class LegacyDB:
    """Thin writer around a fresh SQLite file.

    Deliberately declares *no* foreign keys — the legacy databases have had every FK
    constraint dropped, which is the whole point of the `joins.py` detector.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=OFF")

    def create(
        self,
        table: str,
        columns: Sequence[tuple[str, str]],
        pk: Sequence[str] = (),
    ) -> None:
        cols = ", ".join(f"[{name}] {typ}" for name, typ in columns)
        if pk:
            cols += ", PRIMARY KEY (" + ", ".join(f"[{c}]" for c in pk) + ")"
        self.conn.execute(f"CREATE TABLE [{table}] ({cols})")

    def insert(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        placeholders = ", ".join("?" * len(columns))
        cols = ", ".join(f"[{c}]" for c in columns)
        self.conn.executemany(
            f"INSERT INTO [{table}] ({cols}) VALUES ({placeholders})", rows
        )
        return len(rows)

    def exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def finish(self) -> None:
        self.conn.commit()
        self.conn.execute("VACUUM")
        self.conn.close()


class PgLegacyDB:
    """The Postgres counterpart of `LegacyDB`, writing into a freshly created database.

    Same contract: no foreign keys, ever. Bulk rows go through COPY, which is the only
    way to write the ~16K rentals and payments without the build taking minutes.
    """

    def __init__(self, uri: str):
        import psycopg

        self.conn = psycopg.connect(uri, autocommit=True)

    def create(
        self,
        table: str,
        columns: Sequence[tuple[str, str]],
        pk: Sequence[str] = (),
    ) -> None:
        cols = ", ".join(f'"{name}" {typ}' for name, typ in columns)
        if pk:
            cols += ", PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk) + ")"
        self.conn.execute(f'CREATE TABLE "{table}" ({cols})')

    def insert(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = ", ".join(f'"{c}"' for c in columns)
        with self.conn.cursor() as cur, cur.copy(f'COPY "{table}" ({cols}) FROM STDIN') as cp:
            for row in rows:
                cp.write_row(row)
        return len(rows)

    def comment(self, table: str, column: str | None, text: str) -> None:
        """Anti-pattern #3: documentation, some of it wrong. SQLite has no comments."""
        target = f'"{table}"."{column}"' if column else f'"{table}"'
        kind = "COLUMN" if column else "TABLE"
        literal = text.replace("'", "''")
        self.conn.execute(f"COMMENT ON {kind} {target} IS '{literal}'")

    def exec(self, sql: str, params: Sequence[Any] = ()) -> Any:
        # Only pass params when there are some: given any params at all, psycopg reads
        # every bare % as a placeholder, and `txn_no % 7` is an operator.
        return self.conn.execute(sql, params) if params else self.conn.execute(sql)

    def finish(self) -> None:
        # Statistics, so row estimates in pg_class are real rather than zero: the
        # excavation reads `reltuples` for table sizes and the cost guard reads plans.
        self.conn.execute("VACUUM ANALYZE")
        self.conn.close()


def read_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM [{table}]")]
    conn.row_factory = None
    return rows


def rng(*salt: Any) -> random.Random:
    """A separate deterministic stream per call site, so that adding a transform
    upstream does not shift every downstream random decision."""
    return random.Random(f"{SEED}:{':'.join(str(s) for s in salt)}")
