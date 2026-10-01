"""Detector: overloaded fields (anti-pattern #9).

`REF_ID` is a customer id when `REF_TYP = 'C'` and an employee id when `REF_TYP = 'E'`.
One column, two meanings, chosen by a sibling. Join it without the condition and you
get rows matched to the wrong entity entirely — with no error, because both sides are
integers.

The signature is that the column's *value distribution changes* with the
discriminator: different ranges, different shapes, different lengths. A column whose
distribution is the same under every value of the discriminator is not overloaded, it
is just a column.

Also catches free-text columns carrying structured payloads — `NOTES` holding
`DSC:SUMMER10` — which is the same problem wearing a different hat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..connectors.executor import ReadOnlyExecutor
from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .naming import expand
from .profile import ColumnProfile, ProfileSet

# A prefixed code hiding in free text: DSC:SUMMER10, REF#12345, [URGENT]
EMBEDDED_CODE = re.compile(r"^\s*([A-Z]{2,8})\s*[:#=\-]\s*([A-Z0-9_\-]{2,20})\b")

REFERENCE_WORDS = {"reference", "related", "parent", "source", "target", "link",
                   "owner", "entity", "object"}


@dataclass
class Split:
    discriminator_value: str
    rows: int
    minimum: float | None
    maximum: float | None
    distinct: int


def _discriminator_for(table: Table, column: str, profiles: ProfileSet) -> list[str]:
    """Sibling columns that could select which meaning applies.

    Prefers a column whose name shares the overloaded column's stem — `REF_ID` and
    `REF_TYP` — and otherwise any low-cardinality code column in the same table.
    """
    table_profile = profiles.get(table.qualified)
    if table_profile is None:
        return []
    stem = set(expand(column).words) - {"id", "number", "code", "key"}
    ranked: list[tuple[int, str]] = []
    for other in table.columns:
        if other.name.lower() == column.lower():
            continue
        profile = table_profile.column(other.name)
        if profile is None or not 2 <= profile.distinct_count <= 10:
            continue
        if profile.null_fraction > 0.5:
            continue
        words = set(expand(other.name).words)
        if not words & {"type", "code", "kind", "class", "category"}:
            continue
        shared = bool(stem & words)
        ranked.append((1 if shared else 0, other.name))
    return [name for _, name in sorted(ranked, key=lambda x: -x[0])]


def _distribution_by_value(
    executor: ReadOnlyExecutor, table: Table, column: str, discriminator: str,
    values: list[str],
) -> list[Split]:
    connector = executor.connector
    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    out: list[Split] = []
    for value in values:
        literal = str(value).replace("'", "''")
        probe = (
            f"SELECT COUNT(*), MIN({column}), MAX({column}), COUNT(DISTINCT {column}) "
            f"FROM {target} WHERE {discriminator} = '{literal}' AND {column} IS NOT NULL"
        )
        try:
            result = executor.run(probe, row_cap=2)
        except Exception:
            continue
        if not result.rows or not result.rows[0][0]:
            continue
        rows, minimum, maximum, distinct = result.rows[0]
        try:
            minimum = float(minimum) if minimum is not None else None
            maximum = float(maximum) if maximum is not None else None
        except (TypeError, ValueError):
            minimum = maximum = None
        out.append(Split(str(value), int(rows), minimum, maximum, int(distinct or 0)))
    return out


def _ranges_differ(splits: list[Split]) -> float:
    """How distinct the value ranges are across the discriminator. 0 = identical."""
    usable = [s for s in splits if s.minimum is not None and s.maximum is not None]
    if len(usable) < 2:
        return 0.0
    separations = 0
    pairs = 0
    for index, left in enumerate(usable):
        for right in usable[index + 1:]:
            pairs += 1
            # Disjoint ranges are the clearest evidence; otherwise compare spans.
            if left.maximum < right.minimum or right.maximum < left.minimum:
                separations += 1
                continue
            left_span = max(1.0, left.maximum - left.minimum)
            right_span = max(1.0, right.maximum - right.minimum)
            if max(left_span, right_span) / min(left_span, right_span) > 4:
                separations += 0.5
    return round(separations / pairs, 3) if pairs else 0.0


def _resolve_target(
    executor: ReadOnlyExecutor, schema: Schema, profiles: ProfileSet,
    table: Table, column: str, predicate: str, core_tables: list[Table],
) -> tuple[str, str, float] | None:
    """Which table does the column point at *under this condition*?

    Knowing `REF_ID` is overloaded is only half the finding; the half that makes a
    join possible is which entity each value of the discriminator selects. Test
    inclusion of the column's values, restricted to one meaning, against each
    candidate key, and take the best.
    """
    connector = executor.connector
    source = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)

    best: tuple[str, str, float] | None = None
    for parent in core_tables:
        if parent is table:
            continue
        key = parent.primary_key[0] if parent.primary_key else None
        if key is None:
            profile = profiles.get(parent.qualified)
            if profile is None:
                continue
            unique = [
                c.column for c in profile.columns.values()
                if c.is_unique_in_sample and c.sampled_rows >= parent.row_estimate
                and c.numeric
            ]
            if len(unique) != 1:
                continue
            key = unique[0]

        parent_target = connector.quote(parent.schema, parent.name) if parent.schema \
            else connector.quote(parent.name)
        probe = (
            f"SELECT COUNT(*), SUM(CASE WHEN p.{key} IS NULL THEN 0 ELSE 1 END) "
            f"FROM (SELECT DISTINCT {column} AS v FROM {source} "
            f"      WHERE {predicate} AND {column} IS NOT NULL) c "
            f"LEFT JOIN {parent_target} p ON p.{key} = c.v"
        )
        try:
            result = executor.run(probe, row_cap=2)
        except Exception:
            continue
        if not result.rows or not result.rows[0][0]:
            continue
        total, matched = result.rows[0][0], result.rows[0][1] or 0
        inclusion = matched / total
        if inclusion < 0.99 or total < 3:
            continue
        # Prefer the parent whose key space the values fill most completely: a set of
        # values that covers 90% of the employee table is about employees, not the
        # first 8 albums.
        coverage = total / max(1, parent.row_estimate)
        score = round(min(1.0, 0.5 + coverage), 3)
        if best is None or score > best[2]:
            best = (parent.qualified, key, score)
    return best


def detect(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    table_classes: dict[str, str] | None = None,
    relationships=None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    core_tables = [
        t for t in schema.base_tables
        if classes.get(t.qualified.lower(), "core") == "core"
    ]
    out: list[Finding] = []

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        table_profile = profiles.get(table.qualified)
        if table_profile is None or table_profile.sampled_rows < 20:
            continue

        for column in table.columns:
            profile = table_profile.column(column.name)
            if profile is None or profile.sampled_rows == 0:
                continue

            out.extend(_conditional_reference(
                executor, schema, table, column.name, profile, profiles, core_tables
            ))
            embedded = _embedded_code(table, column.name, profile)
            if embedded is not None:
                out.append(embedded)
    return out


def _conditional_reference(
    executor: ReadOnlyExecutor, schema: Schema, table: Table, column: str,
    profile: ColumnProfile, profiles: ProfileSet, core_tables: list[Table],
) -> list[Finding]:
    words = set(expand(column).words)
    if not (words & REFERENCE_WORDS and words & {"id", "number", "code", "key"}):
        return []
    if profile.distinct_count < 5:
        return []

    for discriminator in _discriminator_for(table, column, profiles)[:2]:
        discriminator_profile = profiles.get(table.qualified, discriminator)
        if discriminator_profile is None:
            continue
        values = [str(v) for v, _ in discriminator_profile.top_values[:8]]
        splits = _distribution_by_value(executor, table, column, discriminator, values)
        if len(splits) < 2:
            continue
        separation = _ranges_differ(splits)
        if separation < 0.5:
            continue

        targets: dict[str, dict] = {}
        for split in splits:
            predicate = f"{discriminator} = '{split.discriminator_value}'"
            resolved = _resolve_target(
                executor, schema, profiles, table, column, predicate, core_tables
            )
            if resolved is not None:
                targets[split.discriminator_value] = {
                    "table": resolved[0], "column": resolved[1],
                    "confidence": resolved[2],
                }
        return [
            Finding(
                kind=Kind.OVERLOADED,
                target=f"{table.qualified}.{column}",
                value={
                    "table": table.qualified,
                    "column": column,
                    "discriminator": discriminator,
                    "meanings": {
                        s.discriminator_value: {
                            "predicate": f"{discriminator} = '{s.discriminator_value}'",
                            "range": [s.minimum, s.maximum],
                            "rows": s.rows,
                            **(
                                {
                                    "target_table": targets[s.discriminator_value]["table"],
                                    "target_column":
                                        targets[s.discriminator_value]["column"],
                                }
                                if s.discriminator_value in targets else {}
                            ),
                        }
                        for s in splits
                    },
                },
                confidence=merge_confidence(0.6, 0.4 * separation),
                evidence={
                    "reason": (
                        f"the values in {column} occupy different ranges depending on "
                        f"{discriminator}, so the column means different things"
                    ),
                    "separation": separation,
                    "resolved_targets": targets,
                    "splits": [
                        {
                            "value": s.discriminator_value, "rows": s.rows,
                            "min": s.minimum, "max": s.maximum, "distinct": s.distinct,
                        }
                        for s in splits
                    ],
                },
                detector="overloaded",
                status=Status.HYPOTHESIS,
                note=(
                    "any join through this column must carry the discriminator "
                    "condition, or rows match the wrong entity"
                ),
            )
        ]
    return []


def _embedded_code(table: Table, column: str, profile: ColumnProfile) -> Finding | None:
    """Free text that also carries a structured code."""
    if profile.numeric or profile.value_shape is None:
        return None
    if profile.value_shape.avg_length < 4:
        return None

    values = [str(v) for v, _ in profile.top_values if v is not None]
    matches = [EMBEDDED_CODE.match(v) for v in values]
    hits = [m for m in matches if m]
    if len(hits) < 2 or not values:
        return None
    rate = len(hits) / len(values)
    if not 0.05 < rate < 0.9:
        return None

    prefixes = sorted({m.group(1) for m in hits})
    return Finding(
        kind=Kind.OVERLOADED,
        target=f"{table.qualified}.{column}",
        value={
            "table": table.qualified,
            "column": column,
            "pattern": f"{prefixes[0]}:<code>",
            "prefixes": prefixes,
            "kind": "embedded_code",
        },
        confidence=round(min(0.8, 0.45 + rate), 3),
        evidence={
            "reason": (
                f"{rate:.0%} of values in this free-text column start with a code "
                f"prefix ({', '.join(prefixes)}), so it carries structured data too"
            ),
            "examples": [v for v in values if EMBEDDED_CODE.match(v)][:5],
            "match_rate": round(rate, 3),
        },
        detector="overloaded",
        status=Status.HYPOTHESIS,
    )
