"""Detector: implicit and composite keys (anti-pattern #10).

Half the tables have no primary key, and some of the ones that matter most are keyed by
convention only: order numbers restart inside each store, so the real identity of an
order is `(STORE_CD, ORD_NO)` and of an order line `(STORE_CD, ORD_NO, LN_SEQ)`.

Getting this right is what stops fan-out. Join order lines to orders on `ORD_NO` alone
and every order silently collects the lines of its namesakes in three other stores; the
revenue figure comes back four times too large and nothing errors.

Search is over the *sample* first — combinations that are not unique in a sample are
certainly not unique in the table — and only surviving candidates cost a `GROUP BY …
HAVING COUNT(*) > 1` probe against the real table.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ..connectors.executor import ReadOnlyExecutor
from .findings import Finding, Kind, Status
from .introspect import Schema, Table
from .naming import expand
from .profile import ProfileSet

# Beyond three columns a "key" is usually just the whole row.
MAX_KEY_WIDTH = 3

# Columns that are never part of an identity even when they happen to be unique.
_NEVER_KEY_WORDS = {
    "amount", "price", "cost", "total", "quantity", "balance", "percent",
    "description", "comment", "note", "notes", "text", "address", "milliseconds",
    "bytes", "timestamp",
}


@dataclass
class KeyCandidate:
    table: Table
    columns: list[str]
    unique_in_sample: bool
    verified_unique: bool | None = None
    null_free: bool = True
    discriminator: str = ""

    @property
    def width(self) -> int:
        return len(self.columns)


def _eligible_columns(table: Table, profiles: ProfileSet) -> list[str]:
    out: list[str] = []
    for column in table.columns:
        profile = profiles.get(table.qualified, column.name)
        if profile is None or profile.sampled_rows == 0:
            continue
        if profile.null_fraction > 0.02:
            continue                      # a key column is not null
        if profile.is_constant:
            continue
        words = set(expand(column.name).words)
        if words & _NEVER_KEY_WORDS:
            continue
        if profile.value_shape is not None and profile.value_shape.avg_length > 40:
            continue                      # free text is not an identity
        out.append(column.name)
    return out


def _sample_unique(
    profiles: ProfileSet, table: Table, columns: list[str], rows_in_scope: int = 0
) -> bool:
    """Is this combination unique across the profiled sample?

    Reconstructed from the per-column profiles when the combination is a single
    column; wider combinations need the rows, which the profile does not keep, so
    they are screened by a cheaper necessary condition: the product of the columns'
    distinct counts has to be at least the row count.
    """
    profile = profiles.get(table.qualified)
    if profile is None or profile.sampled_rows == 0:
        return False
    if len(columns) == 1:
        column_profile = profile.column(columns[0])
        return bool(column_profile and column_profile.is_unique_in_sample)

    # The screen is a necessary condition: n columns cannot identify more rows than
    # the product of their distinct counts. When a discriminator is in play the
    # candidate only has to be unique *within* that partition, so compare against the
    # partition's size — otherwise the real key of a god table is never even probed.
    target = rows_in_scope or profile.sampled_rows
    product = 1
    for name in columns:
        column_profile = profile.column(name)
        if column_profile is None:
            return False
        product *= max(1, column_profile.distinct_count)
        if product >= target:
            return True
    return product >= target


def verify_unique(
    executor: ReadOnlyExecutor, candidate: KeyCandidate
) -> KeyCandidate:
    """The real test: does any combination of these values occur twice?"""
    connector = executor.connector
    table = candidate.table
    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    cols = ", ".join(connector.quote(c) for c in candidate.columns)
    not_null = " AND ".join(f"{connector.quote(c)} IS NOT NULL" for c in candidate.columns)
    where = not_null
    if candidate.discriminator:
        where += f" AND {candidate.discriminator}"
    probe = (
        f"SELECT COUNT(*) FROM (SELECT {cols} FROM {target} WHERE {where} "
        f"GROUP BY {cols} HAVING COUNT(*) > 1) d"
    )
    try:
        result = executor.run(probe, row_cap=2)
        candidate.verified_unique = (result.rows[0][0] == 0) if result.rows else None
    except Exception:
        candidate.verified_unique = None
    return candidate


def _parent_key_sets(table: Table, relationships) -> list[set[str]]:
    """One column set per relationship this table has to a parent.

    Kept separate rather than unioned: a candidate that contains a *whole* upward join
    is evidence of identity, whereas one that clips a column from each of two different
    joins is nothing at all.
    """
    out: list[set[str]] = []
    for finding in relationships or []:
        value = getattr(finding, "value", None) or {}
        if value.get("from_table", "").lower() != table.qualified.lower():
            continue
        columns = {c.lower() for c in value.get("from_columns", [])}
        if columns and columns not in out:
            out.append(columns)
    return out


def detect(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    table_classes: dict[str, str] | None = None,
    discriminators: dict[str, str] | None = None,
    relationships=None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    discriminator_columns = {k.lower(): v for k, v in (discriminators or {}).items()}
    out: list[Finding] = []

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        # A declared primary key is the answer; record it and move on. Checked before
        # the sample-size guard below: two stores and two staff are too few rows to
        # *infer* a key from, but the DDL already says what the key is.
        if table.primary_key:
            out.append(
                Finding(
                    kind=Kind.KEY,
                    target=table.qualified,
                    value={"columns": table.primary_key, "declared": True},
                    confidence=1.0,
                    evidence={"reason": "declared primary key"},
                    detector="keys",
                    status=Status.ACCEPTED,
                )
            )
            continue

        profile = profiles.get(table.qualified)
        if profile is None or profile.sampled_rows < 5:
            continue

        discriminator = ""
        rows_in_scope = 0
        column = discriminator_columns.get(table.qualified.lower())
        if column is not None:
            column_profile = profiles.get(table.qualified, column)
            if column_profile and column_profile.top_values:
                # Identity inside a god table is per-partition; test within the
                # largest one rather than across the whole mixed table.
                value, count = column_profile.top_values[0]
                discriminator = f"{column} = '{value}'"
                # Scale the sampled count up to the whole table, since the screen
                # below compares against real row counts.
                rows_in_scope = max(
                    1,
                    round(count * (table.row_estimate / max(1, column_profile.sampled_rows))),
                )

        eligible = _eligible_columns(table, profiles)
        if not eligible:
            continue

        parent_sets = _parent_key_sets(table, relationships)
        discriminator_column = discriminator.split("=")[0].strip() if discriminator else ""
        # The discriminator is applied as scope, so it must not also appear inside a
        # candidate — within the scope it is constant and would add nothing.
        eligible = [c for c in eligible if c.lower() != discriminator_column.lower()]

        def plausibility(
            combo, _table=table, _sets=parent_sets, _disc=discriminator_column
        ):
            # Bound as defaults rather than captured: the closure is only ever called
            # within this iteration, but relying on that is the kind of thing that
            # breaks the moment someone defers the call.
            return _key_plausibility(tuple(combo), _table, _sets, _disc)

        # Search every width rather than stopping at the first that yields a unique
        # combination. In SO_LN, (ORD_NO, PRD_ID) happens to be unique at width 2 while
        # the real key (STORE_CD, ORD_NO, LN_SEQ) is at width 3; stopping early would
        # take the accident.
        verified: list[list[str]] = []
        for width in range(1, MAX_KEY_WIDTH + 1):
            ranked = sorted(combinations(eligible, width), key=plausibility, reverse=True)
            for combo in ranked[:24]:      # bound the probe budget per table
                columns = list(combo)
                if any(set(v) < set(columns) for v in verified):
                    continue              # a narrower key already covers this one
                if not _sample_unique(profiles, table, columns, rows_in_scope):
                    continue
                candidate = KeyCandidate(
                    table=table, columns=columns, unique_in_sample=True,
                    discriminator=discriminator,
                )
                verify_unique(executor, candidate)
                if candidate.verified_unique:
                    verified.append(columns)
            # A single unique identifier column is definitive; nothing wider can beat it.
            if width == 1 and verified:
                break

        found = max(verified, key=plausibility) if verified else None
        if found and discriminator_column:
            # The table's identity includes the discriminator: the same order number
            # exists as an order, a payment and a refund.
            found = [*found, discriminator_column]

        if not found:
            continue

        composite = len(found) > 1
        out.append(
            Finding(
                kind=Kind.KEY,
                target=table.qualified,
                value={
                    "columns": found,
                    "declared": False,
                    "within": discriminator or None,
                },
                confidence=0.9 if composite else 0.85,
                evidence={
                    "reason": (
                        "no primary key is declared; this is the narrowest combination "
                        "with no duplicate values in the table"
                        + (f", within {discriminator}" if discriminator else "")
                    ),
                    "width": len(found),
                    "rows_checked": table.row_estimate,
                },
                detector="keys",
                note="implicit key — nothing in the schema declares it",
            )
        )
    return out


def _key_plausibility(
    combo: tuple[str, ...],
    table: Table,
    parent_sets: list[set[str]] | None = None,
    discriminator_column: str = "",
) -> float:
    """Prefer combinations that look like identities and sit at the front of the table.

    Legacy tables put their key columns first, and key columns are named for it. The
    strongest signal, when it is available, is that the combination *contains* the
    columns this table joins upwards on: an order line is keyed by its order plus a
    sequence, so a candidate that does not include the order columns is not identity,
    it is a coincidence.
    """
    score = 0.0
    positions = {c.name.lower(): c.ordinal for c in table.columns}
    lowered = {c.lower() for c in combo}
    for name in combo:
        words = set(expand(name).words)
        if words & {"id", "number", "code", "key", "sequence"}:
            score += 1.0
        ordinal = positions.get(name.lower(), 99)
        score += max(0.0, (20 - ordinal) / 20.0)
    score /= len(combo)

    # Containing an entire upward join is evidence of identity, and the wider that
    # join, the stronger it is: an order line is identified by its whole order.
    for columns in parent_sets or []:
        if columns <= lowered:
            score += 0.5 * len(columns)

    # The decisive signal. Legacy schemas put the key at the front of the table, in
    # order, and a combination that is exactly that leading run is nearly always the
    # key — whereas a scattered pair that happens to be unique is nearly always an
    # accident of the data.
    ordinals = sorted(positions.get(c.lower(), 99) for c in combo)
    if discriminator_column:
        ordinals = sorted([*ordinals, positions.get(discriminator_column.lower(), 99)])
    contiguous = ordinals == list(range(ordinals[0], ordinals[0] + len(ordinals)))
    if contiguous:
        score += 2.5 if ordinals[0] == 0 else 0.3

    # Prefer the narrowest key that explains the data, all else being equal.
    score -= 0.4 * (len(combo) - 1)
    return score
