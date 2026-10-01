"""Detector: god tables (anti-pattern #7).

One very wide table holding several unrelated things — orders, returns and payments —
told apart by a code column, with each kind populating a different subset of the
columns and leaving the rest NULL.

The signal is not width on its own. It is that a low-cardinality code column
*partitions the null pattern*: pick any value of `TRANS_TYP_CD` and a specific set of
columns is always populated while another set is always NULL. A column that is merely
correlated with a status will not do that.

Splitting the table matters beyond tidiness: until the discriminator is known, the
composite key `(STORE_CD, ORD_NO)` is not unique, so `joins.py` cannot confirm the
order-line relationship at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .findings import Finding, Kind, Status
from .introspect import Schema, Table
from .naming import expand
from .profile import ProfileSet

# A table narrower than this is not a god table, it is just a table.
MIN_COLUMNS = 12

# A discriminator has to have at least two and not too many values.
MAX_DISCRIMINATOR_VALUES = 12

# How cleanly the null pattern has to split for a column to count as partitioned.
PARTITION_THRESHOLD = 0.95


@dataclass
class Partition:
    value: str
    row_count: int
    always_populated: list[str] = field(default_factory=list)
    always_null: list[str] = field(default_factory=list)
    exclusive: list[str] = field(default_factory=list)   # populated only by this value
    suggested_name: str = ""


@dataclass
class GodTableSplit:
    table: str
    discriminator: str
    partitions: list[Partition]
    exclusivity: float          # how disjoint the populated column sets are
    column_count: int


def _discriminator_candidates(table: Table, profiles: ProfileSet) -> list[str]:
    out: list[str] = []
    for column in table.columns:
        profile = profiles.get(table.qualified, column.name)
        if profile is None or profile.sampled_rows == 0:
            continue
        if not 2 <= profile.distinct_count <= MAX_DISCRIMINATOR_VALUES:
            continue
        if profile.null_fraction > 0.1:
            continue        # a discriminator is populated on every row
        words = set(expand(column.name).words)
        # Prefer names that say "type" or "code"; do not require them.
        weight = 1 if words & {"type", "code", "kind", "class", "category"} else 0
        out.append((weight, column.name))
    return [name for _, name in sorted(out, key=lambda x: (-x[0], x[1]))]


def _null_pattern_by_value(
    executor, table: Table, discriminator: str, values: list[str]
) -> dict[str, tuple[int, dict[str, float]]]:
    """For each discriminator value, the fill rate of every column.

    One query per value: `SELECT COUNT(*), COUNT(col1), COUNT(col2), ...` which is a
    single pass and gives the non-null count of every column at once.
    """
    connector = executor.connector
    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    columns = [c.name for c in table.columns if c.name.lower() != discriminator.lower()]
    if not columns:
        return {}

    out: dict[str, tuple[int, dict[str, float]]] = {}
    select = ", ".join(f"COUNT({connector.quote(c)})" for c in columns)
    for value in values:
        literal = value.replace("'", "''")
        sql = (
            f"SELECT COUNT(*), {select} FROM {target} "
            f"WHERE {connector.quote(discriminator)} = '{literal}'"
        )
        try:
            result = executor.run(sql, row_cap=2)
        except Exception:
            continue
        if not result.rows:
            continue
        row = result.rows[0]
        total = row[0] or 0
        if total == 0:
            continue
        fill = {
            column: round((row[index + 1] or 0) / total, 4)
            for index, column in enumerate(columns)
        }
        out[value] = (total, fill)
    return out


def analyse(executor, table: Table, discriminator: str, values: list[str]) -> GodTableSplit | None:
    patterns = _null_pattern_by_value(executor, table, discriminator, values)
    if len(patterns) < 2:
        return None

    partitions: list[Partition] = []
    populated_sets: list[set[str]] = []
    for value, (count, fill) in patterns.items():
        populated = sorted(c for c, rate in fill.items() if rate >= PARTITION_THRESHOLD)
        empty = sorted(c for c, rate in fill.items() if rate <= 1 - PARTITION_THRESHOLD)
        partitions.append(
            Partition(value=value, row_count=count, always_populated=populated,
                      always_null=empty)
        )
        populated_sets.append(set(populated))

    # Exclusivity: how much of each partition's populated set is unique to it. A real
    # god table has columns that only one kind of row ever uses.
    exclusive_total = 0
    union = set().union(*populated_sets) if populated_sets else set()
    for index, columns in enumerate(populated_sets):
        others = set().union(*(s for i, s in enumerate(populated_sets) if i != index))
        own = columns - others
        exclusive_total += len(own)
        partitions[index].exclusive = sorted(own)
    exclusivity = round(exclusive_total / len(union), 3) if union else 0.0

    if exclusivity < 0.15:
        return None
    return GodTableSplit(
        table=table.qualified,
        discriminator=discriminator,
        partitions=partitions,
        exclusivity=exclusivity,
        column_count=len(table.columns),
    )


def detect(
    executor,
    schema: Schema,
    profiles: ProfileSet,
    table_classes: dict[str, str] | None = None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    out: list[Finding] = []

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        if len(table.columns) < MIN_COLUMNS:
            continue
        profile = profiles.get(table.qualified)
        if profile is None or profile.sampled_rows < 8:
            continue

        # Wide *and* mostly empty is the shape; check the null rate before probing.
        null_rates = [
            c.null_fraction for c in profile.columns.values() if c.sampled_rows
        ]
        if not null_rates or sum(null_rates) / len(null_rates) < 0.25:
            continue

        # Several columns can partition the null pattern — `REF_TYP` correlates with
        # `TRANS_TYP_CD` and splits it almost as well. Evaluate every candidate and
        # keep the cleanest split rather than the first one that happens to work.
        splits: list[GodTableSplit] = []
        for candidate in _discriminator_candidates(table, profiles):
            column_profile = profiles.get(table.qualified, candidate)
            if column_profile is None:
                continue
            values = [str(v) for v, _ in column_profile.top_values[:MAX_DISCRIMINATOR_VALUES]]
            split = analyse(executor, table, candidate, values)
            if split is not None:
                splits.append(split)

        if splits:
            best = max(splits, key=lambda s: (s.exclusivity, len(s.partitions)))
            split, candidate = best, best.discriminator

            entities = {}
            for partition in split.partitions:
                partition.suggested_name = _suggest_name(table, partition)
                entities[partition.value] = {
                    "predicate": f"{candidate} = '{partition.value}'",
                    "rows": partition.row_count,
                    # Columns this partition never populates, which is what the model
                    # compiler removes. Everything else belongs to the entity, even if
                    # only some of its rows use it.
                    "never_used": partition.always_null,
                    "columns": partition.always_populated,
                    "suggested_entity": partition.suggested_name,
                }
            out.append(
                Finding(
                    kind=Kind.GOD_TABLE,
                    target=table.qualified,
                    value={
                        "table": table.qualified,
                        "discriminator": candidate,
                        "entities": entities,
                    },
                    confidence=min(0.93, 0.5 + split.exclusivity),
                    evidence={
                        "column_count": split.column_count,
                        "exclusivity": split.exclusivity,
                        "reason": (
                            f"{candidate} partitions the null pattern: "
                            f"{split.exclusivity:.0%} of populated columns are used by "
                            "exactly one value of it"
                        ),
                        "alternative_discriminators": [
                            {"column": s.discriminator, "exclusivity": s.exclusivity}
                            for s in sorted(splits, key=lambda s: -s.exclusivity)[1:4]
                        ],
                        "partitions": [
                            {
                                "value": p.value,
                                "rows": p.row_count,
                                "populated": p.always_populated[:12],
                                "exclusive": p.exclusive[:12],
                                "always_null": p.always_null[:12],
                            }
                            for p in split.partitions
                        ],
                    },
                    detector="godtable",
                    status=Status.HYPOTHESIS,
                )
            )
    return out


def _suggest_name(table: Table, partition: Partition) -> str:
    """Name a partition from the columns only it populates.

    `PMT_AMT`, `PMT_DT_TXT`, `PMT_MTHD_CD` are exclusive to one value, and they all
    expand to words starting 'payment', so the entity is a payment.
    """
    from collections import Counter

    # Only the columns this partition alone populates carry its identity. Every
    # partition of an order table populates ORD_NO; only refunds populate RTN_RSN_CD.
    words: Counter[str] = Counter()
    for column in (partition.exclusive or partition.always_populated):
        for word in expand(column).words:
            if word not in ("id", "number", "code", "date", "amount", "text", "type",
                            "status", "flag", "name", "value", "timestamp"):
                words[word] += 1
    base = expand(table.name).logical
    if not words:
        return f"{base}_{partition.value.lower()}"
    top = words.most_common(1)[0][0]
    return top.replace(" ", "_")


def discriminator_map(findings: list[Finding]) -> dict[str, str]:
    """{table: discriminator column} for the detectors that need it downstream."""
    return {
        f.value["table"]: f.value["discriminator"]
        for f in findings
        if f.kind is Kind.GOD_TABLE and f.status is not Status.REJECTED
    }
