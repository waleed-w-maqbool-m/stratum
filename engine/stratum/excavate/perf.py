"""Detector: performance traps (anti-pattern #16).

A table with millions of rows and no index on the column every question filters by.
Nothing is wrong with the data; the trap is that an ordinary-looking question — "show
me the audit entries from March" — is a full scan that will sit there for minutes.

This detector does not stop anything. It produces the hints the cost guard uses at
query time, and the "suggest an index" notes the Workbench shows a DBA.
"""

from __future__ import annotations

from dataclasses import dataclass

from .findings import Finding, Kind, Status
from .introspect import Schema, Table
from .naming import expand
from .profile import ProfileSet

# Below this a scan is cheap enough that nobody cares.
BIG_TABLE_ROWS = 100_000

# Column roles that questions filter on, in the order they usually matter.
FILTER_WORDS = ("date", "timestamp", "time", "status", "state", "type", "code", "flag")


@dataclass
class Trap:
    table: str
    rows: int
    unindexed_filter_columns: list[str]
    indexed_columns: list[str]


def _likely_filter_columns(table: Table) -> list[str]:
    ranked: list[tuple[int, str]] = []
    for column in table.columns:
        words = set(expand(column.name).words)
        for rank, word in enumerate(FILTER_WORDS):
            if word in words:
                ranked.append((rank, column.name))
                break
    return [name for _, name in sorted(ranked, key=lambda x: x[0])]


def detect(
    schema: Schema,
    profiles: ProfileSet,
    table_classes: dict[str, str] | None = None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    out: list[Finding] = []

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        rows = table.row_estimate
        if rows < BIG_TABLE_ROWS:
            continue

        indexed = table.indexed_columns()
        filter_columns = _likely_filter_columns(table)
        unindexed = [c for c in filter_columns if c.lower() not in indexed]
        if not unindexed:
            continue

        out.append(
            Finding(
                kind=Kind.PERF,
                target=table.qualified,
                value={
                    "table": table.qualified,
                    "rows": rows,
                    "unindexed_filter_columns": unindexed,
                    "suggested_indexes": [
                        f"CREATE INDEX ix_{table.name.lower()}_{c.lower()} "
                        f"ON {table.qualified} ({c})"
                        for c in unindexed[:3]
                    ],
                },
                confidence=0.9,
                evidence={
                    "reason": (
                        f"{table.name} holds about {rows:,} rows and has no index on "
                        + ", ".join(unindexed[:3])
                        + " — the columns questions about it will filter on"
                    ),
                    "rows": rows,
                    "existing_indexes": sorted(indexed),
                    "cost_guard": (
                        "any query that filters this table on these columns is a full "
                        "scan and will be stopped for approval"
                    ),
                },
                detector="perf",
                status=Status.ACCEPTED,
                note="cost-guard hint; no query is blocked by this finding alone",
            )
        )
    return out


def cost_hints(findings: list[Finding]) -> dict[str, dict]:
    """{table: hint} for the cost guard to consult before running a query."""
    return {
        f.value["table"]: {
            "rows": f.value["rows"],
            "unindexed_filter_columns": f.value["unindexed_filter_columns"],
        }
        for f in findings
        if f.kind is Kind.PERF and f.status is not Status.REJECTED
    }
