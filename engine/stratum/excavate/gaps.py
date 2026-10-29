"""Detector: relationships and structure that should exist but do not.

Every other detector reports what the database contains. This one reports what it seems
to be missing, so a person reviewing a legacy schema can see both. Everything it says is a
suggestion and is labelled as one: none of it is used to answer a question until a reviewer
accepts it, and none of the SQL it writes is ever executed.

Seven kinds of gap, each with its own evidence:

* `undeclared_fk`        a relationship Stratum is confident in, with no constraint behind it
* `broken_relationship`  names match and most values do, but not enough: likely a real key
                         eroded by deleted parent rows
* `dangling_reference`   named like a reference, but most of its values match nothing
* `points_outside_core`  the live parent does not hold the values, an archive or backup does
* `missing_unique`       would be a foreign key, but the parent column holds duplicates
* `missing_lookup`       a coded column with decoded labels and no lookup table behind it
* `isolated_table`       a live table nothing was found to relate to
"""

from __future__ import annotations

from collections import defaultdict

from ..connectors.executor import ReadOnlyExecutor
from .ddl import foreign_key_ddl, lookup_ddl
from .explain_relationship import describe
from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .joins import (
    MIN_INCLUSION,
    MIN_NAME_SIMILARITY,
    MIN_NAME_TO_ACCEPT,
    Candidate,
    Rejected,
    _comparable,
    _is_key_candidate,
    key_form,
    test_inclusion,
)
from .naming import fk_name_score
from .profile import ProfileSet

UNDECLARED_FK = "undeclared_fk"
BROKEN = "broken_relationship"
DANGLING = "dangling_reference"
OUTSIDE_CORE = "points_outside_core"
MISSING_UNIQUE = "missing_unique"
MISSING_LOOKUP = "missing_lookup"
ISOLATED = "isolated_table"

# Fewer distinct values than this say nothing: a column holding 1..4 matches anything.
MIN_VALUES_TESTED = 5

# How many archive and backup parents may be probed to place a dangling reference.
PROBE_BUDGET = 25


def _short(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def _pct(value: float) -> str:
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _is_key(table: Table, columns: list[str]) -> bool:
    wanted = sorted(c.lower() for c in columns)
    if sorted(c.lower() for c in table.primary_key) == wanted:
        return True
    return any(sorted(c.lower() for c in uk) == wanted for uk in table.unique_keys)


def _gap(
    gap_type: str,
    table: str,
    columns: list[str],
    to_table: str,
    to_columns: list[str],
    confidence: float,
    reason: str,
    *,
    fix: str = "",
    blockers: list[str] | None = None,
    **evidence,
) -> Finding:
    return Finding(
        kind=Kind.GAP,
        target=table,
        value={
            "gap_type": gap_type, "table": table, "columns": columns,
            "to_table": to_table, "to_columns": to_columns,
        },
        # A suggestion is never certain, whatever the evidence, so it never auto-accepts.
        confidence=min(0.94, confidence),
        evidence={"reason": reason, "fix": fix, "blockers": blockers or [], **evidence},
        detector="gaps",
        status=Status.HYPOTHESIS,
    )


def _child_key(table: str, columns: list[str]) -> tuple:
    return (table.lower(), tuple(sorted(c.lower() for c in columns)))


def detect(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    join_findings: list[Finding],
    rejected: list[Rejected],
    table_classes: dict[str, str],
    enum_findings: list[Finding],
    *,
    probe_budget: int = PROBE_BUDGET,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in table_classes.items()}
    dialect = schema.dialect
    live = [
        f for f in join_findings
        if f.status is not Status.REJECTED and f.confidence >= 0.5
    ]
    known_children = {
        _child_key(f.value["from_table"], f.value["from_columns"]) for f in live
    }
    # A candidate that is the reverse of an edge already found is not a new finding.
    known_edges = {
        frozenset({
            _child_key(f.value["from_table"], f.value["from_columns"]),
            _child_key(f.value["to_table"], f.value["to_columns"]),
        })
        for f in live
    }

    out: list[Finding] = []

    # ------------------------------------------------ confident, but not enforced
    for f in live:
        evidence = f.evidence
        if evidence.get("origin") not in ("data", "shipped_sql"):
            continue
        value = f.value
        child, parent = schema.get(value["from_table"]), schema.get(value["to_table"])
        if child is None or parent is None or child.is_view:
            continue
        orphans = evidence.get("orphan_count") or 0
        keyed = _is_key(parent, value["to_columns"])
        if evidence.get("parent_filter") and not keyed:
            # Unique only once a discriminator is applied (a god table). The join is real,
            # but no foreign key can reference a key that holds for some rows, so the
            # honest suggestion is about the key, not a constraint that would be refused.
            out.append(_gap(
                MISSING_UNIQUE, child.qualified, value["from_columns"], parent.qualified,
                value["to_columns"], f.confidence,
                f"{_short(child.qualified)}.{', '.join(value['from_columns'])} points at "
                f"{_short(parent.qualified)}.{', '.join(value['to_columns'])}, which is "
                f"unique only where {evidence['parent_filter']}. A foreign key needs a key "
                "that holds for every row, so this join cannot be declared as it stands.",
                blockers=[f"the parent is unique only where {evidence['parent_filter']}"],
                origin=evidence["origin"], inclusion=evidence.get("inclusion"),
                values_tested=evidence.get("distinct_values_tested"),
                parent_filter=evidence["parent_filter"],
            ))
            continue
        blockers: list[str] = []
        if orphans:
            blockers.append(f"{orphans} existing value{'s' if orphans != 1 else ''} "
                            "have no parent row")
        if evidence.get("needs_cast"):
            blockers.append("the two columns are stored as different types; they must "
                            "match before a constraint can be added")
        if not keyed and evidence.get("parent_key_is_unique") is False:
            blockers.append(f"{_short(parent.qualified)}.{', '.join(value['to_columns'])} "
                            "has duplicate values, so it cannot be referenced")
        fix = "" if (
            evidence.get("needs_cast") or evidence.get("parent_key_is_unique") is False
        ) else foreign_key_ddl(
            dialect, child.qualified, value["from_columns"], parent.qualified,
            value["to_columns"], orphans=orphans, parent_has_key=keyed,
        )
        out.append(_gap(
            UNDECLARED_FK, child.qualified, value["from_columns"], parent.qualified,
            value["to_columns"], f.confidence,
            describe(evidence["origin"], {**evidence, "chosen_by": evidence.get("chosen_by")},
                     child.qualified, value["from_columns"], parent.qualified,
                     value["to_columns"]),
            fix=fix, blockers=blockers,
            origin=evidence["origin"], inclusion=evidence.get("inclusion"),
            values_tested=evidence.get("distinct_values_tested"),
            orphan_count=orphans, orphan_samples=evidence.get("orphan_samples") or [],
            cardinality=evidence.get("cardinality"),
        ))

    # ------------------------------------------------ near misses the joins turned down
    best: dict[tuple, Rejected] = {}
    for item in rejected:
        c = item.candidate
        key = _child_key(c.child_table.qualified, c.child_columns)
        if key in known_children or c.tested < MIN_VALUES_TESTED:
            continue
        if frozenset({key, _child_key(c.parent_table.qualified, c.parent_columns)}) \
                in known_edges:
            continue
        # A table's own key failing to match another table says that table is partial,
        # not that the key is a broken reference.
        if sorted(x.lower() for x in c.child_table.primary_key) \
                == sorted(x.lower() for x in c.child_columns):
            continue
        current = best.get(key)
        if current is None or (c.inclusion, c.name_score) > (
            current.candidate.inclusion, current.candidate.name_score
        ):
            best[key] = item

    # Two tables that each hold the other's values, neither unique, are a shared
    # dimension (customer and staff on both rentals and payments), not a missing key.
    mutual = {
        (_child_key(i.candidate.child_table.qualified, i.candidate.child_columns),
         _child_key(i.candidate.parent_table.qualified, i.candidate.parent_columns))
        for i in rejected if i.reason == "parent_not_unique"
    }

    placed: list[tuple[Finding, Candidate]] = []
    for item in best.values():
        c = item.candidate
        child, parent = c.child_table, c.parent_table
        inclusion, name = c.inclusion, c.name_score
        numbers = {
            "inclusion": inclusion, "values_tested": c.tested,
            "orphan_count": c.orphan_count, "orphan_samples": c.orphan_samples,
            "name_similarity": name, "parent_unique": c.parent_unique,
        }
        where = (f"{_short(child.qualified)}.{', '.join(c.child_columns)} -> "
                 f"{_short(parent.qualified)}.{', '.join(c.parent_columns)}")
        examples = (f" (for example {', '.join(c.orphan_samples)})"
                    if c.orphan_samples else "")

        if item.reason == "parent_not_unique":
            reverse = (_child_key(parent.qualified, c.parent_columns),
                       _child_key(child.qualified, c.child_columns))
            if (
                inclusion < MIN_INCLUSION or name < MIN_NAME_TO_ACCEPT
                or reverse in mutual
            ):
                continue
            out.append(_gap(
                MISSING_UNIQUE, child.qualified, c.child_columns, parent.qualified,
                c.parent_columns, min(0.7, merge_confidence(0.4 * inclusion, 0.5 * name)),
                f"{where}: {_pct(inclusion)} of {c.tested} distinct values match, but "
                f"{_short(parent.qualified)}.{', '.join(c.parent_columns)} holds duplicate "
                "values, so it cannot be the parent side of a foreign key. Either that "
                "column is not really a key, or the table has duplicates to clean.",
                blockers=["the parent column is not unique"], **numbers,
            ))
        elif item.reason == "inclusion" and inclusion >= 0.5 \
                and name >= MIN_NAME_TO_ACCEPT:
            finding = _gap(
                BROKEN, child.qualified, c.child_columns, parent.qualified,
                c.parent_columns, min(0.8, merge_confidence(0.4 * inclusion, 0.5 * name)),
                f"{where}: the names match ({name:.2f}) and {_pct(inclusion)} of "
                f"{c.tested} distinct values exist in the parent, but {c.orphan_count} "
                f"do not{examples}. This looks like a real relationship eroded by deleted "
                "parent rows.",
                blockers=[f"{c.orphan_count} values have no parent row"], **numbers,
            )
            out.append(finding)
            placed.append((finding, c))
        elif item.reason == "inclusion" and 0.15 <= inclusion < 0.5 and name >= 0.9 \
                and child is not parent:
            finding = _gap(
                DANGLING, child.qualified, c.child_columns, parent.qualified,
                c.parent_columns, min(0.5, 0.3 + 0.2 * name),
                f"{where}: named like a reference to {_short(parent.qualified)}, but only "
                f"{_pct(inclusion)} of {c.tested} distinct values exist there{examples}.",
                blockers=["most values have no parent row"], **numbers,
            )
            out.append(finding)
            placed.append((finding, c))

    out = _relocate_outside_core(
        executor, schema, profiles, classes, placed, out, probe_budget
    )

    # ------------------------------------------------ coded columns with no lookup table
    for f in enum_findings:
        if f.status is Status.REJECTED or not isinstance(f.value, dict) or len(f.value) < 2:
            continue
        table_name, _, column = f.target.rpartition(".")
        table = schema.get(table_name)
        if table is None or table.is_view or classes.get(table.qualified.lower(), "core") != "core":
            continue
        if _child_key(table.qualified, [column]) in known_children:
            continue
        if [c.lower() for c in table.primary_key] == [column.lower()]:
            continue
        info = table.column(column)
        labels = {str(k): str(v) for k, v in f.value.items()}
        out.append(_gap(
            MISSING_LOOKUP, table.qualified, [column], "", [],
            0.4 + 0.3 * f.confidence,
            f"{_short(table.qualified)}.{column} holds {len(labels)} codes "
            f"({', '.join(list(labels)[:5])}{', ...' if len(labels) > 5 else ''}) that are "
            "decoded here but not by any table in the database.",
            fix=lookup_ddl(dialect, table.qualified, column, info.data_type if info else "",
                           labels),
            codes=len(labels), source=f.evidence.get("source", ""),
        ))

    # ------------------------------------------------ live tables nothing relates to
    touched = {f.value["from_table"].lower() for f in live} | {
        f.value["to_table"].lower() for f in live
    }
    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        if table.qualified.lower() in touched or len(table.columns) < 2:
            continue
        out.append(_gap(
            ISOLATED, table.qualified, [], "", [], 0.3,
            f"No relationship was found to or from {_short(table.qualified)}. It may be a "
            "standalone table, or its links use values Stratum could not match.",
        ))

    order = [UNDECLARED_FK, BROKEN, OUTSIDE_CORE, DANGLING, MISSING_UNIQUE,
             MISSING_LOOKUP, ISOLATED]
    return sorted(out, key=lambda g: (order.index(g.value["gap_type"]), -g.confidence))


def _relocate_outside_core(
    executor: ReadOnlyExecutor,
    schema: Schema,
    profiles: ProfileSet,
    classes: dict[str, str],
    placed: list[tuple[Finding, Candidate]],
    out: list[Finding],
    budget: int,
) -> list[Finding]:
    """Does a dangling column point into an archive or backup instead?

    Rows that were moved out of the live table leave their children pointing at nothing.
    Testing the same column against the tables that were set aside finds them.
    """
    if not placed or budget <= 0:
        return out
    aside = [
        t for t in schema.base_tables
        if classes.get(t.qualified.lower(), "core") != "core"
    ]
    if not aside:
        return out
    wanted = {
        _child_key(c.child_table.qualified, c.child_columns): finding
        for finding, c in placed
    }
    # Only the dangling columns against the set-aside tables' keys: a full candidate
    # generation over every table would spend its time on pairs nobody asked about.
    parents = [
        (table, column.name, key_form(profile, column.data_type))
        for table in aside
        for column in table.columns
        for profile in [profiles.get(table.qualified, column.name)]
        if _is_key_candidate(table, column.name, profile)
    ]
    candidates: list[Candidate] = []
    for _, c in placed:
        if len(c.child_columns) != 1:
            continue
        column = c.child_table.column(c.child_columns[0])
        child_form = key_form(
            profiles.get(c.child_table.qualified, c.child_columns[0]),
            column.data_type if column else "",
        )
        for parent, parent_column, parent_form in parents:
            if not _comparable(child_form, parent_form):
                continue
            score = fk_name_score(c.child_columns[0], parent.name, parent_column)
            if score < MIN_NAME_SIMILARITY:
                continue
            candidates.append(Candidate(
                child_table=c.child_table, child_columns=list(c.child_columns),
                parent_table=parent, parent_columns=[parent_column],
                name_score=score, child_forms=[child_form], parent_forms=[parent_form],
            ))
    candidates.sort(key=lambda c: -c.name_score)

    found: dict[tuple, Candidate] = {}
    for candidate in candidates[:budget]:
        key = _child_key(candidate.child_table.qualified, candidate.child_columns)
        if key in found:
            continue
        test_inclusion(executor, candidate)
        if candidate.tested >= MIN_VALUES_TESTED and candidate.inclusion >= MIN_INCLUSION:
            found[key] = candidate

    for key, candidate in found.items():
        original = wanted[key]
        out.remove(original)
        kind = classes.get(candidate.parent_table.qualified.lower(), "set aside")
        live_parent = original.value["to_table"]
        out.append(_gap(
            OUTSIDE_CORE, candidate.child_table.qualified, candidate.child_columns,
            candidate.parent_table.qualified, candidate.parent_columns,
            min(0.75, merge_confidence(0.4 * candidate.inclusion, 0.4 * candidate.name_score)),
            f"{_short(candidate.child_table.qualified)}."
            f"{', '.join(candidate.child_columns)} does not match "
            f"{_short(live_parent)}, but {_pct(candidate.inclusion)} of "
            f"{candidate.tested} distinct values exist in "
            f"{_short(candidate.parent_table.qualified)}, a {kind} table. Rows may have "
            "been moved out of the live table while their children still point at them.",
            blockers=["the parent rows are in a table Stratum treats as set aside"],
            inclusion=candidate.inclusion, values_tested=candidate.tested,
            orphan_count=candidate.orphan_count, orphan_samples=candidate.orphan_samples,
            name_similarity=candidate.name_score, live_parent=live_parent,
            parent_class=kind,
        ))
    return out


def group_by_table(gaps: list[Finding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for gap in gaps:
        grouped[gap.value["table"]].append(gap)
    return dict(grouped)
