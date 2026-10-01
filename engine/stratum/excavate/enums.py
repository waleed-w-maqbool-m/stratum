"""Detector: magic numbers (anti-pattern #11).

`ORD_STS` holds 1 to 6, and also 9. Nobody wrote down what they mean. Three sources of
truth, in descending order of how much they should be believed:

1. **A CASE expression in shipped SQL.** `procmine.py` already found these, and they
   are not guesses — they are what the reporting layer has been using for a decade.
2. **Co-occurrence.** Rows with status 9 are the only ones with a cancellation date
   populated; that is evidence about what 9 means, derived from the data itself.
3. **An LLM guess**, given the column name and the value distribution. Marked
   `hypothesis` and never applied without review.

This module does 1 and 2. The LLM step is `enrich.py`, which only sees the columns
this one could not decode.
"""

from __future__ import annotations

from dataclasses import dataclass

from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .naming import expand
from .procmine import MinedKnowledge
from .profile import ColumnProfile, ProfileSet

# "state" is deliberately absent: in a legacy schema it is a postal address far more
# often than a status, and treating BILL_ST as an enum fills the review queue with
# American state codes.
CODE_WORDS = {"status", "type", "code", "kind", "class", "category",
              "flag", "reason", "method", "indicator"}

# A column with more distinct values than this is data, not a code.
MAX_ENUM_VALUES = 30


@dataclass
class EnumCandidate:
    table: str
    column: str
    values: dict[str, int]          # value -> row count in the sample
    mapping: dict[str, str]         # value -> label, where known
    source: str
    confidence: float


# Words that mean the column measures something rather than classifying it.
MEASURE_WORDS = {"price", "amount", "cost", "total", "rate", "quantity", "balance",
                 "percent", "duration", "milliseconds", "bytes", "size", "weight"}


def is_enum_shaped(
    column_name: str,
    profile: ColumnProfile,
    *,
    is_foreign_key: bool = False,
    small_table: bool = False,
) -> bool:
    """Low cardinality, not null-heavy, and named or typed like a code.

    Two exclusions matter more than the inclusions. A foreign key is low-cardinality
    against a small parent but its values are identities, not categories — nobody can
    say what `MTYP_ID = 3` "means" without joining. And a price column holding 0.99 and
    1.99 is low-cardinality by accident of the catalogue, not by design.
    """
    if is_foreign_key:
        return False
    if profile.distinct_count > MAX_ENUM_VALUES or profile.distinct_count < 2:
        return False
    # Sparse columns are normal inside a god table, where most columns belong to a
    # different kind of row entirely.
    if profile.null_fraction > 0.98:
        return False

    words = set(expand(column_name).words)
    if words & MEASURE_WORDS:
        return False
    shape = profile.value_shape
    if shape is not None and (
        shape.signatures.get("decimal", 0) > 0.5 or shape.signatures.get("currency", 0) > 0.5
    ):
        return False

    non_null = profile.sampled_rows - profile.null_count
    # Enough observations to believe the cardinality, unless the whole table is small,
    # in which case a handful of rows is all there is.
    if non_null < 20 and not (small_table and profile.distinct_count <= 6):
        return False
    # A code is shared by many rows. When every row has its own value (two staff
    # named Mike and Jon), the column names things; short values do not make it a code.
    if profile.distinct_count >= non_null:
        return False

    if words & CODE_WORDS:
        return True
    # A short fixed-width string column with few values is a code even if its name
    # does not say so: CHAR(1) holding 'I','P','R'.
    if shape is not None and shape.max_length <= 4 and profile.distinct_count <= 12:
        return True
    return bool(profile.numeric and profile.distinct_count <= 12)


def label_by_cooccurrence(
    executor, table: Table, column: str, profile: ColumnProfile, table_profile
) -> dict[str, str]:
    """Infer a label from which other columns a value co-occurs with.

    Rows whose status is 9 are the only ones with `CANCEL_DT` populated, so 9 means
    cancelled. The label is named after the column that gives the value away.

    Equal *counts* are not enough to conclude this — in a 66-row table two unrelated
    columns will coincide regularly, and a wrong enum label is worse than none because
    it reads as knowledge. So a candidate pairing is confirmed with a query: the
    co-occurring column must be populated for essentially every row with this value and
    essentially no row without it.
    """
    out: dict[str, str] = {}
    if table_profile is None or executor is None:
        return out

    connector = executor.connector
    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)

    total = profile.sampled_rows
    for value, count in profile.top_values:
        if count == 0 or count / total > 0.5:
            continue
        candidates = []
        for other_name, other in table_profile.columns.items():
            if other_name == column.lower() or other.sampled_rows == 0:
                continue
            populated = other.sampled_rows - other.null_count
            if populated == 0 or abs(populated - count) > max(1, count * 0.05):
                continue
            words = [
                w for w in expand(other.column).words
                if w not in ("date", "timestamp", "time", "amount", "code", "id",
                             "number", "flag", "text")
            ]
            if words:
                candidates.append((other.column, " ".join(words)))

        # Exactly one plausible partner, verified against the table, or nothing.
        if len(candidates) != 1:
            continue
        other_column, label = candidates[0]
        literal = str(value).replace("'", "''")
        probe = (
            f"SELECT SUM(CASE WHEN {connector.quote(column)} = '{literal}' "
            f"          AND {connector.quote(other_column)} IS NOT NULL THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {connector.quote(column)} = '{literal}' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN ({connector.quote(column)} <> '{literal}' "
            f"          OR {connector.quote(column)} IS NULL) "
            f"          AND {connector.quote(other_column)} IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM {target}"
        )
        try:
            result = executor.run(probe, row_cap=2)
        except Exception:
            continue
        if not result.rows:
            continue
        both, with_value, elsewhere = (result.rows[0][i] or 0 for i in range(3))
        if with_value == 0:
            continue
        if both / with_value >= 0.95 and elsewhere == 0:
            out[str(value)] = label
    return out


def detect(
    schema: Schema,
    profiles: ProfileSet,
    mined: MinedKnowledge | None = None,
    table_classes: dict[str, str] | None = None,
    relationships=None,
    executor=None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}

    # Columns that take part in a detected relationship, on either side.
    key_columns: set[tuple[str, str]] = set()
    for finding in relationships or []:
        value = getattr(finding, "value", None) or {}
        for table, columns in (
            (value.get("from_table", ""), value.get("from_columns", [])),
            (value.get("to_table", ""), value.get("to_columns", [])),
        ):
            for column in columns:
                if table and column:
                    key_columns.add((table.lower(), column.lower()))

    # What shipped SQL decoded, keyed by (table, column).
    from_routines: dict[tuple[str, str], dict[str, str]] = {}
    routine_source: dict[tuple[str, str], str] = {}
    for mined_enum in (mined.enums if mined else []):
        key = (mined_enum.table.lower(), mined_enum.column.lower())
        from_routines.setdefault(key, {}).update(mined_enum.mapping)
        routine_source[key] = mined_enum.source

    out: list[Finding] = []
    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        table_profile = profiles.get(table.qualified)
        if table_profile is None:
            continue

        small_table = table.row_estimate <= 50
        for column in table.columns:
            profile = table_profile.column(column.name)
            if profile is None:
                continue
            if not is_enum_shaped(
                column.name,
                profile,
                is_foreign_key=(table.qualified.lower(), column.name.lower()) in key_columns
                or [c.lower() for c in table.primary_key] == [column.name.lower()],
                small_table=small_table,
            ):
                continue

            key = (table.name.lower(), column.name.lower())
            values = {str(v): c for v, c in profile.top_values}
            mapping = dict(from_routines.get(key, {}))
            signals = []
            reasons = []
            source = "value distribution"

            if mapping:
                signals.append(0.94)
                source = routine_source.get(key, "shipped SQL")
                reasons.append(f"decoded by a CASE expression in {source}")

            inferred = label_by_cooccurrence(
                executor, table, column.name, profile, table_profile
            )
            for value, label in inferred.items():
                if value not in mapping:
                    mapping[value] = label
                    signals.append(0.7)
                    reasons.append(
                        f"every row with {column.name} = {value} has a populated "
                        f"{label} column, and no other row does"
                    )

            undecoded = [v for v in values if v not in mapping]
            if not mapping:
                signals.append(0.35)
                reasons.append(
                    "low-cardinality code column; the values themselves are not "
                    "self-describing"
                )

            out.append(
                Finding(
                    kind=Kind.ENUM,
                    target=f"{table.qualified}.{column.name}",
                    value=mapping,
                    confidence=merge_confidence(*signals) if signals else 0.3,
                    evidence={
                        "distinct_values": profile.distinct_count,
                        "distribution": [[str(v), c] for v, c in profile.top_values[:15]],
                        "undecoded": undecoded,
                        "reasons": reasons,
                        "source": source,
                        "needs_llm": bool(undecoded) and not from_routines.get(key),
                    },
                    detector="enums",
                    status=Status.ACCEPTED if mapping and not undecoded
                    else Status.HYPOTHESIS,
                )
            )
    return out


def enum_map(findings: list[Finding]) -> dict[str, dict[str, str]]:
    """{table.column: {value: label}} for the semantic model and the scorer."""
    return {
        f.target: f.value
        for f in findings
        if f.kind is Kind.ENUM and f.status is not Status.REJECTED and f.value
    }
