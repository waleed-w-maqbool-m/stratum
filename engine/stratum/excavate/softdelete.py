"""Detector: implicit soft deletes (anti-pattern #12).

Rows are not deleted here, they are marked. In four different ways, because four
different people solved it four different times:

* a flag column — `DEL_FLG = 'Y'`
* a status code — `STS = 9`
* a tombstone date — `DLT_DT IS NOT NULL`
* a name prefix — the product is renamed `ZZ_Whatever`

Every one of these produces the same failure if missed: counts that are quietly too
high, and nobody notices because the number is plausible.

The strongest evidence is not the column name. It is that flagged rows *stop being
referenced*: no order placed after the flag date points at a flagged customer. A column
that behaves like that is a delete, whatever it is called.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..connectors.executor import ReadOnlyExecutor
from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .naming import expand
from .procmine import MinedKnowledge
from .profile import ColumnProfile, ProfileSet

FLAG_WORDS = {"deleted", "delete", "inactive", "archived", "void", "cancelled",
              "obsolete", "removed", "disabled", "purged"}
ACTIVE_WORDS = {"active", "current", "live", "enabled", "valid"}
STATUS_WORDS = {"status", "state"}

# Values that mean "this row is gone".
DELETED_VALUES = {"y", "yes", "t", "true", "1", "d", "x"}
LIVE_VALUES = {"n", "no", "f", "false", "0", "a", ""}

# A name prefix convention for retired records.
RETIRED_PREFIX = re.compile(r"^(zz|xx|zzz|del|old)[_\- ]", re.IGNORECASE)


@dataclass
class Rule:
    table: str
    style: str               # flag | status_code | tombstone_date | name_prefix
    column: str
    deleted_predicate: str
    live_predicate: str
    affected_fraction: float
    confidence: float
    reasons: list[str]


def _flag_rule(table: Table, column: str, profile: ColumnProfile) -> Rule | None:
    words = set(expand(column).words)
    is_delete_flag = bool(words & FLAG_WORDS)
    is_active_flag = bool(words & ACTIVE_WORDS)
    if not (is_delete_flag or is_active_flag):
        return None
    if profile.distinct_count > 4:
        return None

    values = {str(v).strip().casefold() for v, _ in profile.top_values}
    if not values & (DELETED_VALUES | LIVE_VALUES):
        return None

    # The flag is mixed-case and sometimes NULL, so both sides need normalising.
    marked = [v for v, _ in profile.top_values
              if str(v).strip().casefold() in DELETED_VALUES]
    if is_delete_flag:
        if not marked:
            return None
        literal = str(marked[0]).strip()
        deleted = f"UPPER(COALESCE({column},'N')) = '{literal.upper()}'"
        live = f"UPPER(COALESCE({column},'N')) <> '{literal.upper()}'"
        affected = sum(c for v, c in profile.top_values
                       if str(v).strip().casefold() in DELETED_VALUES)
    else:
        live_values = [v for v, _ in profile.top_values
                       if str(v).strip().casefold() in DELETED_VALUES]
        if not live_values:
            return None
        literal = str(live_values[0]).strip()
        live = f"UPPER(COALESCE({column},'{literal}')) = '{literal.upper()}'"
        deleted = f"UPPER(COALESCE({column},'{literal}')) <> '{literal.upper()}'"
        affected = profile.sampled_rows - sum(
            c for v, c in profile.top_values
            if str(v).strip().casefold() in DELETED_VALUES
        )

    fraction = affected / profile.sampled_rows if profile.sampled_rows else 0.0
    return Rule(
        table=table.qualified, style="flag", column=column,
        deleted_predicate=deleted, live_predicate=live,
        affected_fraction=round(fraction, 4),
        confidence=0.85,
        reasons=[f"{column} is a two-valued flag named for deletion or activity"],
    )


def _status_rule(table: Table, column: str, profile: ColumnProfile) -> Rule | None:
    """A status code with one rare terminal value.

    `9` in a set that is otherwise `1..6` is the shape: out of sequence, and much
    rarer than its neighbours.
    """
    words = set(expand(column).words)
    if not words & STATUS_WORDS:
        return None
    if not profile.numeric:
        return _lettered_status_rule(table, column, profile)
    if profile.distinct_count > 30 or profile.distinct_count < 2:
        return None

    values = {int(v): c for v, c in profile.top_values if isinstance(v, (int, float))}
    if len(values) < 2:
        return None
    ordinary = sorted(v for v in values if v != max(values))
    terminal = max(values)
    if not ordinary or terminal <= max(ordinary) + 1:
        return None                      # contiguous: nothing stands out
    share = values[terminal] / max(1, sum(values.values()))
    if share > 0.3:
        return None                      # too common to be a delete

    return Rule(
        table=table.qualified, style="status_code", column=column,
        deleted_predicate=f"{column} = {terminal}",
        live_predicate=f"({column} <> {terminal} OR {column} IS NULL)",
        affected_fraction=round(share, 4),
        confidence=0.6,
        reasons=[
            f"{column} = {terminal} sits outside the contiguous run "
            f"{min(ordinary)}..{max(ordinary)} and covers only {share:.1%} of rows"
        ],
    )


def _is_temporal(column: str, profile: ColumnProfile) -> bool:
    """Is this a date, by name, by Hungarian tag, or by its declared type?

    The name alone misses `dtmDltd`, whose only date word is the `dtm` tag, and any
    tombstone named just `DELETED` whose type is TIMESTAMP. The type is the stronger
    evidence anyway: it is what the column actually holds.
    """
    expanded = expand(column)
    if set(expanded.words) & {"date", "timestamp", "time", "datetime"}:
        return True
    if expanded.type_prefix in {"dtm", "dt"}:
        return True
    declared = (profile.declared_type or "").lower()
    return any(word in declared for word in ("date", "time"))


# Single-letter or short status codes that conventionally mean "gone". A subset of
# DELETED_VALUES on purpose: `1`, `y` and `t` are booleans, not statuses.
DELETED_CODES = {"x", "d", "del", "dlt", "deleted", "purged", "void"}


def _lettered_status_rule(table: Table, column: str, profile: ColumnProfile) -> Rule | None:
    """The same shape as a numeric terminal status, written in letters.

    `A`/`I`/`X` — active, inactive, and one rare code nobody documented. The numeric
    rule looks for a value out of sequence; letters have no sequence, so what stands
    out instead is a code whose conventional meaning is deletion, held by few rows.
    """
    if profile.distinct_count > 8 or profile.distinct_count < 2:
        return None
    codes = {str(v).strip(): c for v, c in profile.top_values if v is not None}
    if not codes or any(len(code) > 8 for code in codes):
        return None                      # free text, not a code column
    marked = [code for code in codes if code.casefold() in DELETED_CODES]
    if len(marked) != 1:
        return None                      # none, or ambiguous between two
    literal = marked[0]
    share = codes[literal] / max(1, sum(codes.values()))
    if share > 0.3:
        return None                      # too common to be a delete
    quoted = literal.replace("'", "''")
    return Rule(
        table=table.qualified, style="status_code", column=column,
        deleted_predicate=f"{column} = '{quoted}'",
        live_predicate=f"({column} <> '{quoted}' OR {column} IS NULL)",
        affected_fraction=round(share, 4),
        confidence=0.6,
        reasons=[
            f"{column} = '{literal}' is a status code conventionally meaning deleted, "
            f"held by {share:.1%} of rows among {sorted(codes)}"
        ],
    )


def _tombstone_rule(table: Table, column: str, profile: ColumnProfile) -> Rule | None:
    words = set(expand(column).words)
    if not (words & FLAG_WORDS and _is_temporal(column, profile)):
        return None
    if profile.null_fraction < 0.5:
        return None                      # a tombstone is null for live rows
    return Rule(
        table=table.qualified, style="tombstone_date", column=column,
        deleted_predicate=f"{column} IS NOT NULL",
        live_predicate=f"{column} IS NULL",
        affected_fraction=round(1 - profile.null_fraction, 4),
        confidence=0.85,
        reasons=[
            f"{column} is a deletion date that is NULL on "
            f"{profile.null_fraction:.1%} of rows"
        ],
    )


def _name_prefix_rule(table: Table, column: str, profile: ColumnProfile) -> Rule | None:
    """Products renamed `ZZ_…` when they are withdrawn."""
    if profile.value_shape is None or profile.numeric:
        return None
    words = set(expand(column).words)
    if not words & {"name", "title", "description"}:
        return None
    marked = [v for v, _ in profile.top_values if RETIRED_PREFIX.match(str(v))]
    prefixed = sum(c for v, c in profile.top_values if RETIRED_PREFIX.match(str(v)))
    if not marked or profile.sampled_rows == 0:
        return None
    fraction = prefixed / profile.sampled_rows
    if fraction > 0.5:
        return None                      # it is a naming convention, not a retirement
    prefix = re.match(r"^([A-Za-z]+[_\- ])", str(marked[0]))
    if prefix is None:
        return None
    literal = prefix.group(1)
    return Rule(
        table=table.qualified, style="name_prefix", column=column,
        deleted_predicate=f"{column} LIKE '{literal}%'",
        live_predicate=f"{column} NOT LIKE '{literal}%'",
        affected_fraction=round(fraction, 4),
        confidence=0.55,
        reasons=[f"{fraction:.1%} of values are prefixed {literal!r}"],
    )


def confirm_by_orphaning(
    executor: ReadOnlyExecutor, table: Table, rule: Rule, schema: Schema,
    core_tables: set[str] | None = None,
) -> tuple[bool | None, str]:
    """Do flagged rows stop being referenced?

    The decisive test. If flagged rows are referenced by children just as often as
    live ones, the flag means something else. Uses any child table that the join
    detector would have found by name; when there is none, returns None rather than
    guessing.
    """
    connector = executor.connector
    key = table.primary_key[0] if table.primary_key else None
    if key is None:
        return None, "no primary key to check references against"

    child_table = child_column = None
    for other in schema.base_tables:
        if other is table:
            continue
        # Only a live table can testify. A backup or staging copy still contains the
        # flagged rows *and* their references, so asking it whether deleted rows are
        # orphaned always answers "no" and rejects a correct rule.
        if core_tables is not None and other.qualified.lower() not in core_tables:
            continue
        match = next((c for c in other.columns if c.name.lower() == key.lower()), None)
        if match is not None:
            child_table, child_column = other, match.name
            break
    if child_table is None:
        return None, "no child table references this key"

    target = connector.quote(table.schema, table.name) if table.schema \
        else connector.quote(table.name)
    child_target = connector.quote(child_table.schema, child_table.name) \
        if child_table.schema else connector.quote(child_table.name)
    probe = (
        f"SELECT SUM(CASE WHEN {rule.deleted_predicate} THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN {rule.deleted_predicate} AND EXISTS "
        f"  (SELECT 1 FROM {child_target} ch WHERE ch.{child_column} = t.{key}) "
        f"  THEN 1 ELSE 0 END) "
        f"FROM {target} t"
    )
    try:
        result = executor.run(probe, row_cap=2)
    except Exception as exc:
        return None, f"reference check failed: {exc}"[:200]
    if not result.rows or not result.rows[0][0]:
        return None, "no rows match the rule"

    flagged, referenced = result.rows[0][0] or 0, result.rows[0][1] or 0
    orphan_rate = 1 - (referenced / flagged)
    if orphan_rate >= 0.9:
        return True, (
            f"{orphan_rate:.0%} of flagged rows are never referenced by "
            f"{child_table.name}, against a live population that is"
        )
    return False, (
        f"only {orphan_rate:.0%} of flagged rows are unreferenced — the flag does "
        "not look like a delete"
    )


def detect(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    mined: MinedKnowledge | None = None,
    table_classes: dict[str, str] | None = None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    core_tables = {t for t, k in classes.items() if k == "core"} or None

    # What shipped SQL already excludes, keyed by (table, column).
    routine_filters: dict[tuple[str, str], list] = {}
    for filt in (mined.filters if mined else []):
        routine_filters.setdefault(
            (filt.table.lower(), filt.column.lower()), []
        ).append(filt)

    out: list[Finding] = []
    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        profile = profiles.get(table.qualified)
        if profile is None or profile.sampled_rows < 4:
            continue

        rules: list[Rule] = []
        for column in table.columns:
            column_profile = profile.column(column.name)
            if column_profile is None or column_profile.sampled_rows == 0:
                continue
            for builder in (_flag_rule, _status_rule, _tombstone_rule, _name_prefix_rule):
                rule = builder(table, column.name, column_profile)
                if rule is not None:
                    rules.append(rule)

        for rule in rules:
            signals = [rule.confidence]
            reasons = list(rule.reasons)

            # Shipped SQL that filters this column is near-proof.
            mined_here = routine_filters.get((table.name.lower(), rule.column.lower()))
            if mined_here:
                signals.append(0.9)
                reasons.append(
                    "shipped SQL excludes these rows: "
                    + "; ".join(sorted({m.expression for m in mined_here}))
                )

            confirmed, explanation = confirm_by_orphaning(
                executor, table, rule, schema, core_tables
            )
            if confirmed is True:
                signals.append(0.8)
                reasons.append(explanation)
            elif confirmed is False:
                # Contradicted by the data; do not emit a filter that would hide rows.
                continue
            else:
                reasons.append(explanation)

            out.append(
                Finding(
                    kind=Kind.SOFT_DELETE,
                    target=f"{table.qualified}.{rule.column}",
                    value={
                        "table": table.qualified,
                        "column": rule.column,
                        "style": rule.style,
                        "deleted_predicate": rule.deleted_predicate,
                        "live_predicate": rule.live_predicate,
                    },
                    confidence=merge_confidence(*signals),
                    evidence={
                        "reasons": reasons,
                        "affected_fraction": rule.affected_fraction,
                        "style": rule.style,
                        "rows_affected": int(
                            rule.affected_fraction * (profile.row_estimate or 0)
                        ),
                    },
                    detector="softdelete",
                    status=Status.HYPOTHESIS,
                    note=(
                        "applied as a default filter — every query against this entity "
                        "will exclude these rows unless the user asks for them"
                    ),
                )
            )
    return out
