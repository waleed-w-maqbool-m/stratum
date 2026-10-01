"""Detector: schema drift (anti-pattern #6).

The schema changed underneath the data and nobody backfilled. Two shapes:

**Column replacement.** `PHONE` was deprecated in favour of `PHN_NBR`. Old rows have
one, new rows have the other, and neither column alone answers "what is this
customer's phone number". The signature is *complementary nulls*: the two columns are
almost never both populated and almost never both empty. The fix is a COALESCE.

**Unit change.** Money was stored in dollars until a cutover, and in integer cents
afterwards. The signature is a step change in magnitude across a date boundary, or —
as here — two columns that are populated on opposite sides of a date and differ by a
round factor. Missing this does not produce an error, it produces a revenue figure a
hundred times too large for recent years, which is the worst kind of wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..connectors.executor import ReadOnlyExecutor
from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .naming import entity_words, expand
from .profile import ColumnProfile, ProfileSet

# How cleanly the two columns have to divide the rows between them.
MIN_COMPLEMENTARITY = 0.9

# Ratios that indicate a unit change rather than two unrelated numbers.
UNIT_RATIOS = {100: "cents to dollars", 1000: "thousands", 1_000_000: "millions"}


@dataclass
class Pair:
    table: Table
    old: ColumnProfile
    new: ColumnProfile
    complementarity: float
    both_populated: int
    neither_populated: int


def _candidate_pairs(
    table: Table, profiles: ProfileSet, scope_present: bool = False
) -> list[tuple[ColumnProfile, ColumnProfile]]:
    """Columns that plausibly mean the same thing.

    Requires shared entity words so `PHONE`/`PHN_NBR` pair up but `PHONE`/`FAX_NBR`
    do not, and requires both to be partly null — a replacement always leaves gaps.
    """
    table_profile = profiles.get(table.qualified)
    if table_profile is None:
        return []
    candidates = [
        p for p in table_profile.columns.values()
        if 0.05 < p.null_fraction < 0.95 and p.sampled_rows >= 8
    ]
    out = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            left_words = entity_words(left.column) or set(expand(left.column).words)
            right_words = entity_words(right.column) or set(expand(right.column).words)
            if not (left_words & right_words):
                continue
            # The null fractions have to be roughly complementary before it is worth
            # a query: if both are 10% null they are not two halves of one column.
            # Inside a god table the whole-table fractions are inflated by the other
            # partitions, so the screen is skipped and the query decides.
            if not scope_present and abs(
                (left.null_fraction + right.null_fraction) - 1.0
            ) > 0.25:
                continue
            out.append((left, right))
    return out


def _measure_complementarity(
    executor: ReadOnlyExecutor, table: Table, left: str, right: str, scope: str = ""
) -> tuple[float, int, int, int]:
    """How often exactly one of the two columns is populated."""
    connector = executor.connector
    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    where = f" WHERE {scope}" if scope else ""
    probe = (
        f"SELECT COUNT(*), "
        f"SUM(CASE WHEN {left} IS NOT NULL AND {right} IS NOT NULL THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN {left} IS NULL AND {right} IS NULL THEN 1 ELSE 0 END) "
        f"FROM {target}{where}"
    )
    try:
        result = executor.run(probe, row_cap=2)
    except Exception:
        return 0.0, 0, 0, 0
    if not result.rows or not result.rows[0][0]:
        return 0.0, 0, 0, 0
    total = result.rows[0][0]
    both = result.rows[0][1] or 0
    neither = result.rows[0][2] or 0
    exactly_one = total - both - neither
    return round(exactly_one / total, 4), both, neither, total


def _find_boundary(
    executor: ReadOnlyExecutor, table: Table, old: str, new: str, scope: str = ""
) -> tuple[str, str] | None:
    """The date column and value where the changeover happened, if there is one."""
    connector = executor.connector
    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    # A date by name or by type: a TIMESTAMP column called `created` is as good an
    # ordering as one called `created_date`.
    date_columns = [
        c.name for c in table.columns
        if (
            {"date", "timestamp", "time"} & set(expand(c.name).words)
            or any(t in (c.data_type or "").lower() for t in ("date", "time"))
        )
        and c.name.lower() not in (old.lower(), new.lower())
    ]
    for column in date_columns[:3]:
        probe = (
            f"SELECT MAX(CASE WHEN {old} IS NOT NULL THEN {column} END), "
            f"MIN(CASE WHEN {new} IS NOT NULL THEN {column} END) "
            f"FROM {target} WHERE {column} IS NOT NULL"
            + (f" AND {scope}" if scope else "")
        )
        try:
            result = executor.run(probe, row_cap=2)
        except Exception:
            continue
        if not result.rows:
            continue
        last_old, first_new = result.rows[0]
        if last_old is None or first_new is None:
            continue
        # A clean cutover: the new column starts where the old one stops.
        if str(first_new) >= str(last_old):
            return column, str(first_new)
    return None


def _unit_ratio(old: ColumnProfile, new: ColumnProfile) -> int | None:
    """Do the two columns' magnitudes differ by a round factor?"""
    if not (old.numeric and new.numeric):
        return None
    if old.max_value in (None, 0) or new.max_value in (None, 0):
        return None
    try:
        ratio = float(new.max_value) / float(old.max_value)
    except (TypeError, ZeroDivisionError):
        return None
    for factor in UNIT_RATIOS:
        if 0.4 < ratio / factor < 2.5:
            return factor
    return None


def detect(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    table_classes: dict[str, str] | None = None,
    discriminators: dict[str, str] | None = None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    discriminator_columns = {k.lower(): v for k, v in (discriminators or {}).items()}
    out: list[Finding] = []

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue

        # Inside a god table, a column is NULL both because it drifted and because
        # this row is a different kind of thing entirely. Measure within one partition
        # or the two effects cancel and the drift disappears.
        scope = ""
        column = discriminator_columns.get(table.qualified.lower())
        if column is not None:
            column_profile = profiles.get(table.qualified, column)
            if column_profile and column_profile.top_values:
                scope = f"{column} = '{column_profile.top_values[0][0]}'"

        for left, right in _candidate_pairs(table, profiles, scope_present=bool(scope)):
            complementarity, both, neither, total = _measure_complementarity(
                executor, table, left.column, right.column, scope
            )
            if complementarity < MIN_COMPLEMENTARITY:
                continue

            # The column populated on the older rows is the deprecated one. Null share is
            # only a first guess: the profiler samples the head and the tail equally, so
            # across a clean cutover both columns come out exactly half NULL and the
            # guess is a coin toss. The boundary settles it — try both orientations and
            # keep the one where one column genuinely stops before the other starts.
            old, new = (left, right) if left.null_fraction > right.null_fraction \
                else (right, left)
            boundary = _find_boundary(executor, table, old.column, new.column, scope)
            if boundary is None:
                flipped = _find_boundary(executor, table, new.column, old.column, scope)
                if flipped is not None:
                    old, new, boundary = new, old, flipped
            ratio = _unit_ratio(old, new)
            if ratio is None and boundary is None:
                # No dated cutover to order them by: the larger magnitude is the finer
                # unit, and money moves to cents, not away from them.
                reverse = _unit_ratio(new, old)
                if reverse:
                    old, new, ratio = new, old, reverse

            signals = [0.55 + 0.35 * (complementarity - MIN_COMPLEMENTARITY) / 0.1]
            reasons = [
                f"exactly one of {old.column} and {new.column} is populated on "
                f"{complementarity:.1%} of rows ({both} have both, {neither} neither)"
            ]
            if boundary:
                signals.append(0.7)
                reasons.append(
                    f"the changeover is clean at {boundary[0]} = {boundary[1]}"
                )

            if ratio:
                signals.append(0.6)
                reasons.append(
                    f"the two columns' magnitudes differ by about {ratio}x "
                    f"({UNIT_RATIOS[ratio]}), so this is a unit change, not just a rename"
                )
                resolution = (
                    f"CASE WHEN {boundary[0]} >= '{boundary[1]}' "
                    f"THEN {new.column} / {ratio}.0 ELSE {old.column} END"
                    if boundary
                    else f"COALESCE({new.column} / {ratio}.0, {old.column})"
                )
                kind = "unit_change"
            else:
                resolution = f"COALESCE({new.column}, {old.column})"
                kind = "column_replacement"

            out.append(
                Finding(
                    kind=Kind.DRIFT,
                    target=f"{table.qualified}.{new.column}",
                    value={
                        "table": table.qualified,
                        "kind": kind,
                        "old": old.column,
                        "new": new.column,
                        "resolution": resolution,
                        "boundary_column": boundary[0] if boundary else None,
                        "boundary_value": boundary[1] if boundary else None,
                        "ratio": ratio,
                    },
                    confidence=merge_confidence(*signals),
                    evidence={
                        "reasons": reasons,
                        "complementarity": complementarity,
                        "rows": total,
                        "old_null_fraction": old.null_fraction,
                        "new_null_fraction": new.null_fraction,
                    },
                    detector="drift",
                    status=Status.HYPOTHESIS,
                    note=(
                        "asking for this field without the resolution returns NULL for "
                        "half the history"
                    ),
                )
            )
    return out


# --------------------------------------------------------- cross-table relocation


def _shape_signature(profile: ColumnProfile) -> str:
    """The dominant value shape, used to tell whether two columns hold the same kind of
    thing. An email column and a phone column are both text and are not the same."""
    shape = profile.value_shape
    if shape is None or not shape.signatures:
        return ""
    return shape.dominant


def detect_relocation(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    relationships,
    table_classes: dict[str, str] | None = None,
) -> list[Finding]:
    """A column that moved to another table partway through the history.

    `CUST_MSTR.EML` is populated for the customers created before the contact table
    existed and NULL for everyone after; their addresses live in `CUST_CNTCT.CNTCT_VAL`
    instead. Neither column answers "what is this customer\'s email" on its own, and
    asking for the one in the obvious place silently returns NULL for half of them.

    The signature is the same complementarity the in-table detector looks for, measured
    across a relationship: the rows where the original column is NULL are exactly the
    rows with a child row carrying the value, and the rows where it is populated have
    none.
    """
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    out: list[Finding] = []

    # Children that point at each table, taken from the relationships already inferred.
    children: dict[str, list[tuple[str, list[str], list[str]]]] = {}
    for finding in relationships or []:
        value = getattr(finding, "value", None) or {}
        parent = value.get("to_table", "")
        if not parent:
            continue
        children.setdefault(parent.lower(), []).append(
            (
                value.get("from_table", ""),
                value.get("from_columns", []),
                value.get("to_columns", []),
            )
        )

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        table_profile = profiles.get(table.qualified)
        if table_profile is None or table_profile.sampled_rows < 8:
            continue
        related = children.get(table.qualified.lower(), [])
        if not related:
            continue

        for column in table.columns:
            profile = table_profile.column(column.name)
            if profile is None or profile.numeric:
                continue
            if not 0.05 < profile.null_fraction < 0.95:
                continue
            signature = _shape_signature(profile)
            if not signature or signature in ("text", "empty"):
                continue          # too generic to match another column on

            found = False
            for child_table, child_columns, parent_columns in related:
                if found:
                    break
                child = schema.get(child_table)
                if child is None or not child_columns or not parent_columns:
                    continue
                child_profile = profiles.get(child.qualified)
                if child_profile is None:
                    continue

                for candidate in child.columns:
                    candidate_profile = child_profile.column(candidate.name)
                    if candidate_profile is None or candidate_profile.numeric:
                        continue
                    # The child column has to hold the same *kind* of value, or a
                    # contact table\'s phone column matches its email column.
                    if _shape_signature(candidate_profile) != signature:
                        continue

                    finding = _test_relocation(
                        executor, table, column.name, profile,
                        child, candidate.name, child_columns[0], parent_columns[0],
                        child_profile,
                    )
                    if finding is not None:
                        out.append(finding)
                        found = True
                        break
    return out


def _test_relocation(
    executor: ReadOnlyExecutor,
    table: Table,
    column: str,
    profile: ColumnProfile,
    child: Table,
    child_column: str,
    child_key: str,
    parent_key: str,
    child_profile,
) -> Finding | None:
    """Confirm the relocation with a query, and find its discriminator if it has one."""
    connector = executor.connector
    parent_target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    child_target = connector.quote(child.schema, child.name) if child.schema \
        else connector.quote(child.name)

    # A contact table holds emails *and* phones, told apart by a type column. Try the
    # unfiltered case first, then each value of a low-cardinality column.
    discriminators: list[str] = [""]
    for other in child.columns:
        other_profile = child_profile.column(other.name)
        if other_profile is None or not 2 <= other_profile.distinct_count <= 8:
            continue
        if other.name.lower() in (child_column.lower(), child_key.lower()):
            continue
        for value, _ in other_profile.top_values[:6]:
            literal = str(value).replace("\'", "\'\'")
            discriminators.append(f"c.{other.name} = \'{literal}\'")

    best: tuple[float, str, int] | None = None
    for discriminator in discriminators:
        condition = f"c.{child_key} = t.{parent_key} AND c.{child_column} IS NOT NULL"
        if discriminator:
            condition += f" AND {discriminator}"
        probe = (
            "SELECT "
            f"SUM(CASE WHEN t.{column} IS NULL THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN t.{column} IS NULL AND EXISTS "
            f"    (SELECT 1 FROM {child_target} c WHERE {condition}) THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN t.{column} IS NOT NULL THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN t.{column} IS NOT NULL AND EXISTS "
            f"    (SELECT 1 FROM {child_target} c WHERE {condition}) THEN 1 ELSE 0 END) "
            f"FROM {parent_target} t"
        )
        try:
            result = executor.run(probe, row_cap=2)
        except Exception:
            continue
        if not result.rows:
            continue
        nulls, nulls_covered, populated, populated_covered = (
            result.rows[0][i] or 0 for i in range(4)
        )
        if nulls == 0 or populated == 0:
            continue

        coverage = nulls_covered / nulls
        leakage = populated_covered / populated
        # The child must cover the gap and *only* the gap. If it also carries values for
        # rows that already have one, this is a second address, not a relocation.
        score = coverage - leakage
        if best is None or score > best[0]:
            best = (score, discriminator, nulls_covered)

    if best is None or best[0] < 0.85:
        return None

    score, discriminator, covered = best
    lookup = (
        f"SELECT MAX(c.{child_column}) FROM {child.name} c "
        f"WHERE c.{child_key} = {table.name}.{parent_key}"
        + (f" AND {discriminator}" if discriminator else "")
    )
    resolution = f"COALESCE({column}, ({lookup}))"
    relocated_to = f"{child.qualified}.{child_column}"
    if discriminator:
        relocated_to += " WHERE " + discriminator.replace("c.", "")

    return Finding(
        kind=Kind.DRIFT,
        target=f"{table.qualified}.{column}",
        value={
            "table": table.qualified,
            "kind": "column_relocation",
            "old": f"{table.qualified}.{column}",
            "new": relocated_to,
            "resolution": resolution,
            "boundary_column": None,
            "boundary_value": None,
            "ratio": None,
        },
        confidence=round(min(0.9, 0.5 + score * 0.45), 3),
        evidence={
            "reasons": [
                f"{covered:,} of the rows where {column} is NULL have the value in "
                f"{child.name}.{child_column} instead, and the rows that already have "
                f"one do not \u2014 so {column} moved to {child.name} partway through"
            ],
            "coverage_minus_leakage": round(score, 3),
            "discriminator": discriminator,
            "null_fraction": profile.null_fraction,
        },
        detector="drift",
        status=Status.HYPOTHESIS,
        note=(
            f"asking for {column} without this lookup returns NULL for "
            f"{profile.null_fraction:.0%} of rows"
        ),
    )
