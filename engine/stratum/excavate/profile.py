"""Step 2 of excavation: what the data actually looks like.

Sampled and time-boxed. For each column: null rate, cardinality, top values, extremes,
length statistics and the pattern signatures from `patterns.py`. Every later detector
reads these profiles rather than the database, so this is the only step that costs
real query time.

The sampling strategy is one query per table, not one per column: a legacy database has
tens of thousands of columns and a round trip per column would take hours.
"""

from __future__ import annotations

import datetime as _dt
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..connectors.executor import ReadOnlyExecutor
from .introspect import Schema, Table
from .patterns import ValueShape, extract_structure, shape

DEFAULT_SAMPLE = 2000
TOP_K = 25


@dataclass
class ColumnProfile:
    table: str
    column: str
    declared_type: str
    sampled_rows: int = 0
    null_count: int = 0
    null_fraction: float = 0.0
    distinct_count: int = 0
    distinct_fraction: float = 0.0
    top_values: list[tuple[Any, int]] = field(default_factory=list)
    min_value: Any = None
    max_value: Any = None
    numeric: bool = False
    temporal: bool = False           # native date/time values, not date-shaped text
    value_shape: ValueShape | None = None
    structure_format: str = ""
    structure_paths: dict[str, str] = field(default_factory=dict)
    python_types: dict[str, int] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def is_constant(self) -> bool:
        return self.distinct_count <= 1 and self.sampled_rows > 0

    @property
    def is_unique_in_sample(self) -> bool:
        non_null = self.sampled_rows - self.null_count
        return non_null > 0 and self.distinct_count == non_null

    @property
    def is_low_cardinality(self) -> bool:
        """Enum-shaped: few distinct values, and not just because the sample is tiny."""
        non_null = self.sampled_rows - self.null_count
        return 1 < self.distinct_count <= 30 and non_null >= 20

    def sample_values(self, n: int = 3) -> list[Any]:
        """The handful of values an LLM is allowed to see (CLAUDE.md #2)."""
        return [v for v, _ in self.top_values[:n]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "type": self.declared_type,
            "null_fraction": self.null_fraction,
            "distinct": self.distinct_count,
            "min": _jsonable(self.min_value),
            "max": _jsonable(self.max_value),
            "top_values": [[_jsonable(v), c] for v, c in self.top_values[:10]],
            "dominant_shape": self.value_shape.dominant if self.value_shape else "",
            "structure": self.structure_format,
        }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass
class TableProfile:
    table: str
    sampled_rows: int
    row_estimate: int
    columns: dict[str, ColumnProfile] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""

    def column(self, name: str) -> ColumnProfile | None:
        return self.columns.get(name.lower())


@dataclass
class ProfileSet:
    tables: dict[str, TableProfile] = field(default_factory=dict)
    elapsed_ms: int = 0
    skipped: list[str] = field(default_factory=list)

    def get(self, table: str, column: str | None = None):
        tp = self.tables.get(table.lower())
        if tp is None or column is None:
            return tp
        return tp.column(column)

    def all_columns(self):
        for tp in self.tables.values():
            yield from tp.columns.values()


def profile_table(
    executor: ReadOnlyExecutor,
    table: Table,
    sample_size: int = DEFAULT_SAMPLE,
) -> TableProfile:
    """Two queries at most, then everything is computed in Python.

    A true random sample means a full scan on a big legacy table, which is exactly what
    the cost guard exists to prevent. So the sample is taken from both *ends* of the
    table instead: cheap on every engine that can read in reverse physical order, and
    unbiased in the one way that matters here, which is old rows versus new ones.

    That matters more than it sounds. A head-only sample of an order-line table reads
    2000 rows belonging to one order and concludes the order column is constant; a
    head-only sample of a table mid-migration sees none of the new column at all.
    """
    started = time.perf_counter()
    out = TableProfile(table=table.qualified, sampled_rows=0, row_estimate=table.row_estimate)
    if not table.columns:
        return out

    connector = executor.connector
    half = max(1, sample_size // 2) if table.row_estimate > sample_size else sample_size
    sql = connector.sample_sql(table.schema, table.name, table.column_names, half)
    try:
        result = executor.run(sql, row_cap=half, skip_cap=True)
    except Exception as exc:
        out.error = str(exc)
        out.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return out

    rows = list(result.rows)
    if table.row_estimate > sample_size:
        tail_sql = connector.sample_sql_tail(
            table.schema, table.name, table.column_names, half
        )
        if tail_sql:
            try:
                tail = executor.run(tail_sql, row_cap=half, skip_cap=True)
                rows.extend(tail.rows)
            except Exception:
                pass      # the head sample alone is still usable

    out.sampled_rows = len(rows)
    by_index = {i: name for i, name in enumerate(result.columns)}
    columns_by_name = {c.name.lower(): c for c in table.columns}

    for index, name in by_index.items():
        declared = columns_by_name.get(name.lower())
        values = [row[index] for row in rows]
        out.columns[name.lower()] = _profile_column(
            table.qualified, name, declared.data_type if declared else "", values
        )
    out.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return out


def _profile_column(
    table: str, name: str, declared_type: str, values: list[Any]
) -> ColumnProfile:
    profile = ColumnProfile(
        table=table, column=name, declared_type=declared_type, sampled_rows=len(values)
    )
    non_null = [v for v in values if v is not None]
    profile.null_count = len(values) - len(non_null)
    profile.null_fraction = round(profile.null_count / len(values), 4) if values else 0.0

    type_counts: Counter[str] = Counter(type(v).__name__ for v in non_null)
    profile.python_types = dict(type_counts)

    if not non_null:
        return profile

    # Hashability is not guaranteed (bytes and memoryview turn up in legacy blobs).
    try:
        counts = Counter(non_null)
        profile.distinct_count = len(counts)
        profile.top_values = counts.most_common(TOP_K)
    except TypeError:
        stringified = Counter(str(v) for v in non_null)
        profile.distinct_count = len(stringified)
        profile.top_values = stringified.most_common(TOP_K)

    profile.distinct_fraction = round(profile.distinct_count / len(non_null), 4)

    # Decimal counts: Postgres NUMERIC and SQL Server DECIMAL arrive as Decimal, and
    # leaving them out profiled every money column on those servers as *text* — which
    # is how a replacement cost ended up trimmed and "spell-corrected" from 9.99 to 19.99.
    numeric = [
        v for v in non_null
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
    ]
    profile.numeric = len(numeric) == len(non_null)
    # Native dates and timestamps are values, not strings shaped like dates. SQLite
    # never produced these, so every date rule assumed text and sliced it with SUBSTR.
    profile.temporal = all(
        isinstance(v, (_dt.date, _dt.datetime, _dt.time)) for v in non_null
    )
    if numeric:
        profile.min_value = min(numeric)
        profile.max_value = max(numeric)
    else:
        try:
            ordered = sorted(str(v) for v in non_null)
            profile.min_value, profile.max_value = ordered[0], ordered[-1]
        except (TypeError, ValueError):
            pass

    profile.value_shape = shape(values)
    if profile.value_shape.looks_structured():
        fmt, paths = extract_structure([str(v) for v in non_null])
        profile.structure_format = fmt
        profile.structure_paths = paths
    return profile


def profile_schema(
    executor: ReadOnlyExecutor,
    schema: Schema,
    *,
    sample_size: int = DEFAULT_SAMPLE,
    time_budget_s: float = 180.0,
    skip_empty: bool = True,
    on_progress=None,
) -> ProfileSet:
    """Profile every table, newest-and-biggest first, inside a time budget.

    Ordering matters: if the budget runs out we want to have profiled the tables the
    query pipeline will actually retrieve, not ninety empty `tmp_` tables.
    """
    started = time.perf_counter()
    out = ProfileSet()

    def priority(table: Table) -> tuple:
        name = table.name.lower()
        junk = name.startswith(("tmp_", "stg_", "bak_", "wrk_", "cnv_", "zz_"))
        return (junk, table.is_view, -table.row_estimate)

    ordered = sorted(schema.tables.values(), key=priority)
    for index, table in enumerate(ordered):
        if time.perf_counter() - started > time_budget_s:
            out.skipped.extend(t.qualified for t in ordered[index:])
            break
        if skip_empty and table.row_estimate == 0 and not table.is_view:
            # Still record it — an empty table is itself a finding for artifacts.py.
            out.tables[table.qualified.lower()] = TableProfile(
                table=table.qualified, sampled_rows=0, row_estimate=0
            )
            continue
        profile = profile_table(executor, table, sample_size)
        out.tables[table.qualified.lower()] = profile
        if on_progress is not None:
            on_progress(index + 1, len(ordered), table.qualified)

    out.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return out
