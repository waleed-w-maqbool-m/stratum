"""Detector: relationships nobody declared (anti-pattern #2).

Every foreign key has been dropped, so the join graph has to be rebuilt from evidence.
Four signals, in increasing order of cost:

1. **Name similarity** on the *expanded* names, so `CUST_NO` and `CUST_ID` are seen as
   the same idea even though their spellings barely overlap (see `naming.py`).
2. **Type compatibility, including cast-compatibility.** `CUST_ID INT` against
   `CUST_NO VARCHAR(10)` holding `'0000000012'` is a real join; it just needs a cast.
3. **Inclusion**: are the child's values a subset of the parent's? Run as a real query
   against a bounded sample of distinct child values.
4. **Parent uniqueness**: the parent side has to be a key, or it is not a foreign key,
   it is a many-to-many.

Anything mined from shipped SQL by `procmine.py` is taken as given and only verified,
never re-derived — a join written in a working view outranks any statistic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..connectors.executor import ReadOnlyExecutor
from ..origin import Origin
from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .naming import expand, fk_name_score
from .procmine import MinedKnowledge
from .profile import ColumnProfile, ProfileSet

# How many distinct child values to test inclusion on. Enough to be conclusive,
# small enough that the probe is cheap on a large table.
INCLUSION_SAMPLE = 400

# Below this, a candidate is not worth a database round trip.
MIN_NAME_SIMILARITY = 0.5

# Inclusion alone cannot carry a relationship. Dense surrogate keys include into each
# other for free: every small integer column is a subset of ALBM_ID's 1..347. So a
# candidate must also be named as though it points at the parent, unless its evidence
# is of a kind that does not happen by accident (see `score`).
MIN_NAME_TO_ACCEPT = 0.7

# How close a losing parent has to be before the choice counts as unresolved rather
# than decided. Wide, because the cases that matter are exact ties.
RIVAL_MARGIN = 0.15

# Inclusion below this is not a relationship, it is a coincidence.
MIN_INCLUSION = 0.85

_DIGITS = re.compile(r"^\d+$")


@dataclass
class KeyForm:
    """How a column's values have to be written to compare against another column."""

    kind: str                 # "int" | "text" | "padded_int" | "other"
    needs_cast: bool = False

    def express(self, qualified: str, target: KeyForm) -> str:
        """SQL for this column, coerced into `target`'s comparison domain."""
        if self.kind == target.kind:
            return qualified
        if {self.kind, target.kind} <= {"int", "padded_int"}:
            # Compare numerically; the padded side is all digits so the cast is safe.
            return f"CAST({qualified} AS INTEGER)" if self.kind == "padded_int" else qualified
        if self.kind == "text" and target.kind in ("int", "padded_int"):
            return f"CAST({qualified} AS INTEGER)"
        return qualified


def key_form(profile: ColumnProfile | None, declared_type: str) -> KeyForm:
    declared = (declared_type or "").upper()
    if profile is None:
        if any(t in declared for t in ("INT", "SERIAL", "NUMERIC", "DECIMAL")):
            return KeyForm("int")
        return KeyForm("text")

    if profile.numeric:
        return KeyForm("int")
    shape = profile.value_shape
    if shape is not None:
        if shape.signatures.get("zero_padded_int", 0) > 0.5:
            return KeyForm("padded_int", needs_cast=True)
        digit_like = shape.signatures.get("integer", 0) + shape.signatures.get(
            "zero_padded_int", 0
        )
        if digit_like > 0.9:
            return KeyForm("padded_int", needs_cast=True)
    return KeyForm("text")


def _comparable(left: KeyForm, right: KeyForm) -> bool:
    if left.kind == right.kind:
        return True
    return {left.kind, right.kind} <= {"int", "padded_int", "text"} and "text" not in (
        left.kind, right.kind
    ) or {left.kind, right.kind} == {"padded_int", "int"}


@dataclass
class Candidate:
    child_table: Table
    child_columns: list[str]
    parent_table: Table
    parent_columns: list[str]
    name_score: float
    child_forms: list[KeyForm] = field(default_factory=list)
    parent_forms: list[KeyForm] = field(default_factory=list)
    inclusion: float = 0.0
    tested: int = 0
    parent_unique: bool | None = None
    from_routine: bool = False
    join_expression: str = ""
    parent_filter: str = ""      # god-table discriminator applied to the parent side
    hierarchy: bool = False      # a self-referencing chain such as MGR_ID -> EMP_ID
    origin: str = Origin.DATA.value
    sources: list[str] = field(default_factory=list)          # routines that wrote the join
    corroborated_by: list[str] = field(default_factory=list)  # other origins that agree
    fk_name: str = ""
    enforced: bool | None = None      # declared only: does the engine check this constraint
    orphan_count: int = 0             # distinct tested values with no parent row
    orphan_samples: list[str] = field(default_factory=list)
    probe_failed: bool = False

    @property
    def composite(self) -> bool:
        return len(self.child_columns) > 1

    @property
    def parent_coverage(self) -> float:
        """Share of the parent's rows this child's distinct values reach.

        The counterweight to inclusion. Inclusion asks whether the parent contains the
        child's values, which a large parent almost always does; this asks whether the
        child has anything to say about the parent at all.

        Used to break a tie between two parents, never to reject one. Real foreign keys
        are routinely sparse — three support reps among ten employees, most customers
        with no invoice — so a low share is not evidence against. It is only better than
        nothing when two candidates are otherwise indistinguishable.
        """
        rows = self.parent_table.row_estimate or 0
        if rows <= 0 or self.tested <= 0:
            return 1.0
        return min(1.0, self.tested / rows)

    def key(self) -> tuple:
        return (
            self.child_table.qualified.lower(),
            tuple(sorted(c.lower() for c in self.child_columns)),
            self.parent_table.qualified.lower(),
            tuple(sorted(c.lower() for c in self.parent_columns)),
        )

    def reversed_key(self) -> tuple:
        child_t, child_c, parent_t, parent_c = self.key()
        return (parent_t, parent_c, child_t, child_c)


def _is_key_candidate(table: Table, column: str, profile: ColumnProfile | None) -> bool:
    """Could this column be the parent side of a foreign key?"""
    if [c.lower() for c in table.primary_key] == [column.lower()]:
        return True
    if any([c.lower() for c in uk] == [column.lower()] for uk in table.unique_keys):
        return True
    if profile is None or profile.sampled_rows == 0:
        return False
    # Unique across the sample, and the sample covered the whole table.
    return profile.is_unique_in_sample and profile.sampled_rows >= table.row_estimate


def _looks_like_identifier(name: str) -> bool:
    words = set(expand(name).words)
    return bool(words & {"id", "number", "code", "key", "reference"})


# Words that name a self-referencing hierarchy rather than another entity.
HIERARCHY_WORDS = {"manager", "parent", "supervisor", "reports", "owner", "predecessor"}


def _hierarchy_reference(
    child_column: str, child: Table, parent: Table, parent_column: str
) -> bool:
    """`MGR_ID` inside the employee table pointing at `EMP_ID`.

    A management chain never shares a name with the key it points at, so the ordinary
    entity-name test cannot see it. What identifies it is that the column names a
    hierarchical role and the parent is the table's own key.
    """
    if child is not parent:
        return False
    if [c.lower() for c in parent.primary_key] != [parent_column.lower()]:
        return False
    return bool(set(expand(child_column).words) & HIERARCHY_WORDS)


def generate_candidates(
    schema: Schema,
    profiles: ProfileSet,
    core_tables: list[Table],
) -> list[Candidate]:
    """Cheap signals only. Every survivor costs a database round trip, so be strict."""
    out: list[Candidate] = []

    # --- parent side: single-column key candidates in core tables
    parents: list[tuple[Table, str, KeyForm]] = []
    for table in core_tables:
        for column in table.columns:
            profile = profiles.get(table.qualified, column.name)
            if _is_key_candidate(table, column.name, profile):
                parents.append((table, column.name, key_form(profile, column.data_type)))

    for child in core_tables:
        for column in child.columns:
            child_profile = profiles.get(child.qualified, column.name)
            if child_profile is not None and child_profile.is_constant:
                continue
            child_f = key_form(child_profile, column.data_type)
            if child_f.kind == "other":
                continue
            own_primary_key = [c.lower() for c in child.primary_key] == [
                column.name.lower()
            ]
            for parent, parent_column, parent_f in parents:
                if parent is child and parent_column.lower() == column.name.lower():
                    continue
                if not _comparable(child_f, parent_f):
                    continue
                score = fk_name_score(column.name, parent.name, parent_column)
                # A table's own primary key is occasionally also a foreign key, but
                # only in a 1:1 extension. Require the names to match outright before
                # believing it, or every surrogate key points at every other one.
                if own_primary_key and score < 1.0:
                    continue
                cross_type = child_f.kind != parent_f.kind
                hierarchy = _hierarchy_reference(column.name, child, parent, parent_column)
                # Inclusion is only evidence for a column that holds identifiers. A
                # rental *time* of '221533' sits inside a 400,000-row log's dense key,
                # and a rental *duration* of 3 to 7 inside every surrogate key there
                # is. Foreign keys are named for what they are — id, number, code, key,
                # reference — and every true edge in both answer keys is.
                if not (hierarchy or _looks_like_identifier(column.name)):
                    continue
                if score < MIN_NAME_SIMILARITY and not (cross_type or hierarchy):
                    continue
                if hierarchy:
                    score = max(score, 0.75)
                out.append(
                    Candidate(
                        child_table=child, child_columns=[column.name],
                        parent_table=parent, parent_columns=[parent_column],
                        name_score=score, child_forms=[child_f], parent_forms=[parent_f],
                        hierarchy=hierarchy,
                    )
                )

    out.extend(_composite_candidates(core_tables, profiles))
    return out


def _composite_candidates(
    core_tables: list[Table], profiles: ProfileSet
) -> list[Candidate]:
    """Tables that share two or more identically named columns.

    This is how `(STORE_CD, ORD_NO)` is found: order numbers restart per store, so
    neither column joins on its own and only the pair does.
    """
    out: list[Candidate] = []
    for child in core_tables:
        child_names = {c.name.lower(): c.name for c in child.columns}
        for parent in core_tables:
            if parent is child:
                continue
            shared = sorted(set(child_names) & {c.name.lower() for c in parent.columns})
            if not 2 <= len(shared) <= 4:
                continue
            # Only worth testing when no single shared column is already unique in the
            # parent — otherwise the single-column candidate covers it.
            single_key = any(
                _is_key_candidate(parent, name, profiles.get(parent.qualified, name))
                for name in shared
            )
            if single_key:
                continue
            columns = [child_names[name] for name in shared]
            out.append(
                Candidate(
                    child_table=child, child_columns=columns,
                    parent_table=parent, parent_columns=columns,
                    name_score=0.8,
                    child_forms=[
                        key_form(profiles.get(child.qualified, c), "") for c in columns
                    ],
                    parent_forms=[
                        key_form(profiles.get(parent.qualified, c), "") for c in columns
                    ],
                )
            )
    return out


def _quote(executor: ReadOnlyExecutor, table: Table) -> str:
    return executor.connector.quote(table.schema, table.name) if table.schema \
        else executor.connector.quote(table.name)


def join_condition(
    candidate: Candidate, child_alias: str = "c", parent_alias: str = "p"
) -> str:
    """The ON clause, with whatever casts the two sides need to be comparable."""
    parts = []
    for index, (child_col, parent_col) in enumerate(
        zip(candidate.child_columns, candidate.parent_columns, strict=False)
    ):
        child_f = candidate.child_forms[index] if index < len(candidate.child_forms) \
            else KeyForm("text")
        parent_f = candidate.parent_forms[index] if index < len(candidate.parent_forms) \
            else KeyForm("text")
        left = child_f.express(f"{child_alias}.{child_col}", parent_f)
        right = parent_f.express(f"{parent_alias}.{parent_col}", child_f)
        parts.append(f"{left} = {right}")
    return " AND ".join(parts)


def test_inclusion(executor: ReadOnlyExecutor, candidate: Candidate) -> Candidate:
    """Are the child's distinct values present in the parent?

    Left-joins a bounded sample of distinct child tuples against the parent and counts
    how many found a match. The sample is of *distinct* values so that one very common
    value cannot carry the result.
    """
    child = _quote(executor, candidate.child_table)
    parent = _quote(executor, candidate.parent_table)
    child_cols = ", ".join(f"c0.{c}" for c in candidate.child_columns)
    not_null = " AND ".join(f"c0.{c} IS NOT NULL" for c in candidate.child_columns)
    profile = executor.profile if hasattr(executor, "profile") else executor.connector.profile

    limit_clause = f"LIMIT {INCLUSION_SAMPLE}" if profile.limit else ""
    top_clause = f"TOP {INCLUSION_SAMPLE} " if profile.top else ""
    sample = (
        f"SELECT DISTINCT {top_clause}{child_cols} FROM {child} c0 "
        f"WHERE {not_null} {limit_clause}"
    )
    condition = join_condition(candidate)
    if candidate.parent_filter:
        condition += f" AND p.{candidate.parent_filter}"
    probe = (
        f"SELECT COUNT(*), SUM(CASE WHEN p.{candidate.parent_columns[0]} IS NULL "
        f"THEN 0 ELSE 1 END) "
        f"FROM ({sample}) c LEFT JOIN {parent} p ON {condition}"
    )
    try:
        result = executor.run(probe, row_cap=2)
    except Exception:
        candidate.inclusion = 0.0
        candidate.probe_failed = True
        return candidate

    if not result.rows:
        return candidate
    total, matched = result.rows[0][0] or 0, result.rows[0][1] or 0
    candidate.tested = int(total)
    candidate.inclusion = round(matched / total, 4) if total else 0.0
    candidate.orphan_count = int(total) - int(matched)
    if candidate.orphan_count > 0:
        candidate.orphan_samples = _orphan_samples(
            executor, candidate, sample, condition, profile
        )
    return candidate


# How many unmatched values to keep for a reviewer. A handful, as for every sample.
ORPHAN_SAMPLES = 3


def _orphan_samples(
    executor: ReadOnlyExecutor, candidate: Candidate, sample: str, condition: str, profile
) -> list[str]:
    """A few child values that found no parent, so "212 orphans" can be shown, not just counted.

    These go to the reviewer's screen and nowhere else; they are never part of a prompt.
    """
    top = f"TOP {ORPHAN_SAMPLES} " if profile.top else ""
    limit = f" LIMIT {ORPHAN_SAMPLES}" if profile.limit else ""
    columns = ", ".join(f"c.{col}" for col in candidate.child_columns)
    query = (
        f"SELECT {top}{columns} FROM ({sample}) c "
        f"LEFT JOIN {_quote(executor, candidate.parent_table)} p ON {condition} "
        f"WHERE p.{candidate.parent_columns[0]} IS NULL{limit}"
    )
    try:
        rows = executor.run(query, row_cap=ORPHAN_SAMPLES).rows
    except Exception:
        return []
    return [
        " / ".join(str(v)[:40] for v in row) for row in rows[:ORPHAN_SAMPLES]
    ]


def test_parent_uniqueness(executor: ReadOnlyExecutor, candidate: Candidate) -> Candidate:
    """A parent key that is not unique means this is not a foreign key."""
    parent = _quote(executor, candidate.parent_table)
    cols = ", ".join(candidate.parent_columns)
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in candidate.parent_columns)
    profile = executor.connector.profile
    where = not_null
    if candidate.parent_filter:
        where += f" AND {candidate.parent_filter}"
    inner = (
        f"SELECT {cols} FROM {parent} WHERE {where} "
        f"GROUP BY {cols} HAVING COUNT(*) > 1"
    )
    probe = f"SELECT COUNT(*) FROM ({inner}) d"
    try:
        result = executor.run(probe, row_cap=2)
        candidate.parent_unique = (result.rows[0][0] == 0) if result.rows else None
    except Exception:
        candidate.parent_unique = None
    del profile
    return candidate


def score(candidate: Candidate) -> float:
    if candidate.origin == Origin.DECLARED.value:
        # The catalog says so. A constraint the engine is not enforcing is weaker.
        return 1.0 if candidate.enforced is not False else 0.97
    if candidate.from_routine:
        return 0.96
    if candidate.inclusion < MIN_INCLUSION:
        return 0.0
    if candidate.parent_unique is False:
        return 0.0

    needs_cast = any(f.needs_cast for f in candidate.child_forms) or any(
        f.needs_cast for f in candidate.parent_forms
    )
    # Evidence that does not happen by chance, and so can stand in for a name that
    # does not match: a composite agreement across several columns at once, or values
    # that line up only after a cast between two different storage types.
    surprising = candidate.composite or (needs_cast and candidate.inclusion >= 0.99)
    if candidate.hierarchy:
        surprising = True

    if candidate.name_score < MIN_NAME_TO_ACCEPT and not surprising:
        return 0.0
    low_cardinality = (
        candidate.tested < 20 and not candidate.composite and candidate.name_score < 0.9
    )
    if low_cardinality and not surprising:
        # Too few distinct values to conclude anything on their own: a column holding
        # 1..8 is a subset of almost every key in the database. An exact name match is
        # the exception — small lookup tables are exactly where that happens.
        return 0.0

    inclusion_signal = (candidate.inclusion - MIN_INCLUSION) / (1 - MIN_INCLUSION)
    signals = [0.5 * inclusion_signal, 0.6 * candidate.name_score]
    if candidate.parent_unique:
        signals.append(0.4)
    if candidate.composite:
        signals.append(0.45)
    if needs_cast and candidate.inclusion >= 0.99:
        signals.append(0.35)
    confidence = merge_confidence(*signals)
    if low_cardinality:
        # The evidence is real but thin — three support-rep ids that happen to cast
        # onto three employee ids. Worth proposing, not worth applying unreviewed.
        return min(0.65, confidence)
    return confidence


_SOURCE_LABEL = {
    Origin.DECLARED.value: "declared foreign key",
    Origin.SHIPPED_SQL.value: "shipped SQL",
    Origin.DATA.value: "inclusion test",
}

# The detector name stays what it was for the two older origins, so findings reviewed
# before `origin` existed keep matching their saved decisions.
_DETECTOR = {
    Origin.DECLARED.value: "declared",
    Origin.SHIPPED_SQL.value: "joins",
    Origin.DATA.value: "joins",
}


def _cardinality(candidate: Candidate, profiles: ProfileSet) -> tuple[str, bool, float | None]:
    """How many children per parent, and whether a child may have none.

    Read from the profiles, so it costs no query. A child column that is itself unique is
    one-to-one (a 1:1 extension table); otherwise many children share a parent. Optional
    means the column is sometimes null, so the child does not always have a parent.
    """
    if candidate.composite:
        return "many_to_one", False, None
    column = candidate.child_columns[0]
    profile = profiles.get(candidate.child_table.qualified, column)
    if profile is None or profile.sampled_rows == 0:
        return "many_to_one", False, None
    unique = _is_key_candidate(candidate.child_table, column, profile)
    fraction = round(profile.null_fraction, 4)
    return ("one_to_one" if unique else "many_to_one"), fraction > 0, fraction


def detect(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    mined: MinedKnowledge | None = None,
    table_classes: dict[str, str] | None = None,
    discriminators: dict[str, str] | None = None,
    declared: list[Finding] | None = None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    core_tables = [
        t for t in schema.tables.values()
        if not t.is_view and classes.get(t.qualified.lower(), "core") == "core"
    ]

    candidates = generate_candidates(schema, profiles, core_tables)

    # A god table's composite key is only unique once the discriminator is applied:
    # (STORE_CD, ORD_NO) repeats across the order, payment and refund rows that share
    # an order, and is a key only within TRANS_TYP_CD = 'I'.
    discriminator_columns = {k.lower(): v for k, v in (discriminators or {}).items()}
    for candidate in candidates:
        column = discriminator_columns.get(candidate.parent_table.qualified.lower())
        if column is None:
            continue
        parent_profile = profiles.get(candidate.parent_table.qualified, column)
        if parent_profile is None or not parent_profile.top_values:
            continue
        dominant = parent_profile.top_values[0][0]
        candidate.parent_filter = f"{column} = '{dominant}'"

    by_key: dict[tuple, Candidate] = {}
    for candidate in candidates:
        by_key.setdefault(candidate.key(), candidate)

    def column_form(table: Table, column: str) -> KeyForm:
        info = table.column(column)
        return key_form(profiles.get(table.qualified, column), info.data_type if info else "")

    # What the catalog declares outranks everything else and is taken as given.
    for finding in (declared or []):
        value = finding.value
        child, parent = schema.get(value["from_table"]), schema.get(value["to_table"])
        if child is None or parent is None:
            continue
        candidate = Candidate(
            child_table=child, child_columns=list(value["from_columns"]),
            parent_table=parent, parent_columns=list(value["to_columns"]),
            name_score=1.0,
            child_forms=[column_form(child, c) for c in value["from_columns"]],
            parent_forms=[column_form(parent, c) for c in value["to_columns"]],
            join_expression=value.get("expression", ""),
            origin=Origin.DECLARED.value,
            fk_name=finding.evidence.get("fk_name", ""),
            enforced=finding.evidence.get("enforced"),
        )
        by_key[candidate.key()] = candidate

    # Fold in what shipped SQL already told us, and mark those as verified-by-code.
    for mined_join in (mined.joins if mined else []):
        child = schema.get(mined_join.left_table)
        parent = schema.get(mined_join.right_table)
        if child is None or parent is None:
            continue
        # Orient so the unique side is the parent.
        child_unique = _is_key_candidate(
            child, mined_join.left_columns[0],
            profiles.get(child.qualified, mined_join.left_columns[0]),
        )
        if child_unique:
            child, parent = parent, child
            child_cols, parent_cols = mined_join.right_columns, mined_join.left_columns
        else:
            child_cols, parent_cols = mined_join.left_columns, mined_join.right_columns
        candidate = Candidate(
            child_table=child, child_columns=child_cols,
            parent_table=parent, parent_columns=parent_cols,
            name_score=1.0, from_routine=True,
            child_forms=[key_form(profiles.get(child.qualified, c), "") for c in child_cols],
            parent_forms=[key_form(profiles.get(parent.qualified, c), "")
                          for c in parent_cols],
            join_expression=mined_join.expression,
            origin=Origin.SHIPPED_SQL.value,
            sources=[mined_join.source],
        )
        prior = by_key.get(candidate.key()) or by_key.get(candidate.reversed_key())
        if prior is not None and prior.origin != Origin.DATA.value:
            # Already known, from the catalog or from another routine: note who else
            # wrote it down rather than replacing the stronger record.
            if mined_join.source not in prior.sources:
                prior.sources.append(mined_join.source)
            if (
                prior.origin == Origin.DECLARED.value
                and Origin.SHIPPED_SQL.value not in prior.corroborated_by
            ):
                prior.corroborated_by.append(Origin.SHIPPED_SQL.value)
            continue
        if prior is not None:
            by_key.pop(prior.key(), None)
        by_key[candidate.key()] = candidate

    def tested_core(candidate: Candidate) -> bool:
        return all(
            classes.get(t.qualified.lower(), "core") == "core"
            for t in (candidate.child_table, candidate.parent_table)
        )

    out: list[Finding] = []
    for candidate in by_key.values():
        if candidate.origin == Origin.DATA.value:
            test_inclusion(executor, candidate)
            if candidate.inclusion < MIN_INCLUSION:
                continue
            test_parent_uniqueness(executor, candidate)
        elif tested_core(candidate):
            # Given joins are not re-derived, but one probe says whether the data
            # honours them, and how many values it does not.
            test_inclusion(executor, candidate)
            if (
                candidate.tested
                and candidate.inclusion >= MIN_INCLUSION
                and Origin.DATA.value not in candidate.corroborated_by
            ):
                candidate.corroborated_by.append(Origin.DATA.value)
        confidence = score(candidate)
        if confidence < 0.5:
            continue
        measured = candidate.tested > 0 and not candidate.probe_failed
        cardinality, optional, null_fraction = _cardinality(candidate, profiles)
        out.append(
            Finding(
                kind=Kind.JOIN,
                target=candidate.child_table.qualified,
                value={
                    "from_table": candidate.child_table.qualified,
                    "from_columns": candidate.child_columns,
                    "to_table": candidate.parent_table.qualified,
                    "to_columns": candidate.parent_columns,
                    "expression": candidate.join_expression
                    or join_condition(candidate, "child", "parent"),
                    "needs_cast": any(f.needs_cast for f in candidate.child_forms)
                    or any(f.needs_cast for f in candidate.parent_forms),
                },
                confidence=confidence,
                evidence={
                    "origin": candidate.origin,
                    "corroborated_by": candidate.corroborated_by,
                    "sources": candidate.sources,
                    "fk_name": candidate.fk_name,
                    "enforced": candidate.enforced,
                    "inclusion": candidate.inclusion,
                    "distinct_values_tested": candidate.tested,
                    "inclusion_measured": measured,
                    "orphan_count": candidate.orphan_count if measured else None,
                    "orphan_samples": candidate.orphan_samples,
                    "name_similarity": candidate.name_score,
                    "parent_coverage": round(candidate.parent_coverage, 4),
                    "parent_key_is_unique": candidate.parent_unique,
                    "composite": candidate.composite,
                    "needs_cast": any(f.needs_cast for f in candidate.child_forms)
                    or any(f.needs_cast for f in candidate.parent_forms),
                    "cardinality": cardinality,
                    "optional": optional,
                    "child_null_fraction": null_fraction,
                    "source": _SOURCE_LABEL[candidate.origin],
                },
                detector=_DETECTOR[candidate.origin],
                status=Status.HYPOTHESIS if candidate.origin == Origin.DATA.value
                else Status.ACCEPTED,
            )
        )
    return _dedupe(out)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Keep the most confident finding per (child, child cols) pair.

    A child column can only really point at one parent; when inclusion holds against
    several, the name similarity is what decides.
    """
    best: dict[tuple, Finding] = {}
    contenders: dict[tuple, list[Finding]] = {}
    for finding in findings:
        value = finding.value
        key = (
            value["from_table"].lower(),
            tuple(sorted(c.lower() for c in value["from_columns"])),
        )
        contenders.setdefault(key, []).append(finding)
        current = best.get(key)
        if current is None or _preferable(finding, current):
            best[key] = finding

    # A column that matched several parents equally well has not been resolved by the
    # data, whichever one won the tiebreak. Carry the losers so enrichment can be asked
    # the question the values cannot answer, and so a reviewer sees what else fit.
    for key, winner in best.items():
        if winner.evidence.get("origin") == "declared":
            continue
        rivals = [
            f.value["to_table"]
            for f in contenders.get(key, [])
            if f is not winner and f.confidence >= winner.confidence - RIVAL_MARGIN
        ]
        if rivals:
            winner.evidence["rival_parents"] = sorted(set(rivals))
            # The parent's key travels with the parent, so record which columns each
            # rival was matched on. Repointing the edge without them would keep the
            # winner's column name against the rival's table.
            winner.evidence["rival_parent_columns"] = {
                f.value["to_table"]: f.value["to_columns"]
                for f in contenders.get(key, [])
                if f is not winner
            }

    return sorted(best.values(), key=lambda f: -f.confidence)


def _preferable(candidate: Finding, incumbent: Finding) -> bool:
    """Is this the better parent for a column that matched more than one?

    Confidence decides when it separates them. When it does not — two casts, two clean
    inclusions, the same score — the tie goes to the parent the child covers more of,
    rather than to whichever was generated first.
    """
    if candidate.confidence != incumbent.confidence:
        return candidate.confidence > incumbent.confidence
    return (
        candidate.evidence.get("parent_coverage", 0.0)
        > incumbent.evidence.get("parent_coverage", 0.0)
    )
