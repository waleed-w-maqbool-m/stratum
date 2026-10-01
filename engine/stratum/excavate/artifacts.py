"""Detector: table sprawl and dead artifacts (anti-patterns #4 and #5).

A legacy database is mostly not the database. Yearly archives, staging tables from a
migration nobody finished, `tmp_` tables with somebody's initials in the name, backup
copies taken before a release in 2014, and views that reference columns dropped years
ago. Retrieval has to exclude all of it or the LLM will confidently query `SO_HDR_2019`
and return last decade's answer.

Classification is deliberately conservative: a table is only demoted out of `core` on
positive evidence, because wrongly hiding a real table makes questions unanswerable
while wrongly keeping a dead one only costs a little retrieval noise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .findings import Finding, Kind, Status, merge_confidence
from .introspect import Schema, Table
from .profile import ProfileSet

CORE = "core"
ARCHIVE = "archive"
STAGING = "staging"
BACKUP = "backup"
DEAD = "dead"

# Name evidence. Each entry is (pattern, class, confidence).
NAME_RULES: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"^tmp[_\W]|^temp[_\W]|_tmp$|^t_\d", re.I), DEAD, 0.9),
    (re.compile(r"^(zz|xx)[_\W]", re.I), DEAD, 0.85),
    (re.compile(r"_(bak|backup|copy|orig|old|prev)(\d*)$", re.I), BACKUP, 0.9),
    (re.compile(r"^(bak|old)[_\W]", re.I), BACKUP, 0.85),
    (re.compile(r"_(bak|backup|copy)_\d{6,8}$", re.I), BACKUP, 0.95),
    (re.compile(r"^(stg|stage|staging|ld|load|ifc|intf)[_\W]", re.I), STAGING, 0.9),
    (re.compile(r"_(stg|staging|load|feed|in|inbound)$", re.I), STAGING, 0.75),
    (re.compile(r"^(rpt|report|snap|snapshot)[_\W]", re.I), ARCHIVE, 0.8),
    (re.compile(r"^(hist|arch|archive)[_\W]|_(hist|archive)$", re.I), ARCHIVE, 0.85),
    (re.compile(r"_(19|20)\d{2}$", re.I), ARCHIVE, 0.9),           # SO_HDR_2019
    (re.compile(r"_(19|20)\d{2}(0[1-9]|1[0-2])$", re.I), ARCHIVE, 0.9),
    (re.compile(r"^(wrk|work|scratch|dedupe|fix|conv|cnv)[_\W]", re.I), DEAD, 0.75),
    (re.compile(r"_(tbd|unused|obsolete|deprecated|delete_?me)$", re.I), DEAD, 0.9),
    (re.compile(r"^(xref_unused|lookup_tbd)$", re.I), DEAD, 0.9),
]

# A date-suffixed sibling of a live table is a backup even if the suffix rule above
# already classified it as an archive; this catches CUST_MSTR_BAK_20240312.
_DATE_SUFFIX = re.compile(r"^(?P<stem>.+?)[_\W](?:bak|backup|copy|old)?[_\W]?(?P<date>(19|20)\d{6})$", re.I)
_YEAR_SUFFIX = re.compile(r"^(?P<stem>.+?)[_\W](?P<year>(19|20)\d{2})$", re.I)


@dataclass
class TableClassification:
    table: str
    klass: str
    confidence: float
    reasons: list[str]


def _name_evidence(name: str) -> tuple[str | None, float, str]:
    for pattern, klass, confidence in NAME_RULES:
        if pattern.search(name):
            return klass, confidence, f"name matches /{pattern.pattern}/"
    return None, 0.0, ""


def _column_jaccard(left: Table, right: Table) -> float:
    a = {c.name.lower() for c in left.columns}
    b = {c.name.lower() for c in right.columns}
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_live_sibling(table: Table, schema: Schema) -> Table | None:
    """The table a backup or archive is a copy of, if it still exists."""
    for pattern in (_DATE_SUFFIX, _YEAR_SUFFIX):
        match = pattern.match(table.name)
        if not match:
            continue
        stem = match.group("stem")
        for candidate in schema.tables.values():
            if candidate.name.lower() == stem.lower() and candidate is not table:
                return candidate
    # `CUST_MSTR_OLD` -> `CUST_MSTR`
    trimmed = re.sub(r"[_\W](old|bak|backup|copy|orig|prev)$", "", table.name, flags=re.I)
    if trimmed.lower() != table.name.lower():
        for candidate in schema.tables.values():
            if candidate.name.lower() == trimmed.lower():
                return candidate
    return None


def broken_views(schema: Schema, profiles: ProfileSet) -> list[Finding]:
    """A view that cannot be selected from is dead by definition."""
    out: list[Finding] = []
    for table in schema.views:
        profile = profiles.get(table.qualified)
        # A view over a dropped column fails two ways: the sample errors, or the
        # catalog cannot even describe it and it comes back with no columns at all.
        unreadable = table.columns == []
        if not unreadable and (profile is None or not profile.error):
            continue
        out.append(
            Finding(
                kind=Kind.TABLE_CLASS,
                target=table.qualified,
                value=DEAD,
                confidence=0.97,
                evidence={
                    "reason": "the view does not execute",
                    "error": (profile.error[:300] if profile and profile.error
                              else "the view's columns cannot be described"),
                    "definition": (table.info.definition or "")[:400],
                },
                detector="artifacts",
                status=Status.ACCEPTED,
                note="broken view — references objects or columns that no longer exist",
            )
        )
    return out


def detect(schema: Schema, profiles: ProfileSet) -> list[Finding]:
    """Classify every table as core / archive / staging / backup / dead."""
    out: list[Finding] = []
    broken = {f.target.lower() for f in broken_views(schema, profiles)}
    out.extend(broken_views(schema, profiles))

    live_tables = [t for t in schema.base_tables if t.row_estimate > 0]

    for table in schema.tables.values():
        if table.qualified.lower() in broken:
            continue

        reasons: list[str] = []
        signals: list[float] = []
        klass: str | None = None

        name_class, name_confidence, name_reason = _name_evidence(table.name)
        if name_class:
            klass = name_class
            signals.append(name_confidence)
            reasons.append(name_reason)

        profile = profiles.get(table.qualified)
        rows = profile.row_estimate if profile else table.row_estimate

        # An empty base table is not necessarily dead — it may be a lookup nobody has
        # populated yet — but combined with any other signal it settles the question.
        if rows == 0 and not table.is_view:
            signals.append(0.55)
            reasons.append("zero rows")
            klass = klass or DEAD

        # A near-duplicate of a live table, with fewer rows, is a copy of it.
        sibling = _find_live_sibling(table, schema)
        if sibling is not None:
            overlap = _column_jaccard(table, sibling)
            if overlap >= 0.5:
                sibling_rows = sibling.row_estimate
                shrunk = rows < sibling_rows
                signals.append(0.9 if shrunk else 0.75)
                reasons.append(
                    f"schema overlaps {sibling.name} by {overlap:.0%} "
                    f"({rows:,} rows vs {sibling_rows:,})"
                )
                if klass is None or klass == DEAD:
                    klass = BACKUP if _DATE_SUFFIX.match(table.name) else (klass or ARCHIVE)

        # A table whose columns are a strict subset of a much larger live table, with a
        # small fraction of its rows, is a staging or snapshot copy.
        if klass is None and not table.is_view and rows:
            for other in live_tables:
                if other is table or other.row_estimate < rows * 3:
                    continue
                mine = {c.name.lower() for c in table.columns}
                theirs = {c.name.lower() for c in other.columns}
                if mine and mine <= theirs and len(mine) >= 3:
                    klass = STAGING
                    signals.append(0.6)
                    reasons.append(
                        f"columns are a subset of {other.name}, which has "
                        f"{other.row_estimate:,} rows to this table's {rows:,}"
                    )
                    break

        if klass is None:
            out.append(
                Finding(
                    kind=Kind.TABLE_CLASS,
                    target=table.qualified,
                    value=CORE,
                    confidence=0.8,
                    evidence={"reason": "no sprawl or staleness signal", "rows": rows},
                    detector="artifacts",
                    status=Status.ACCEPTED,
                )
            )
            continue

        out.append(
            Finding(
                kind=Kind.TABLE_CLASS,
                target=table.qualified,
                value=klass,
                confidence=merge_confidence(*signals),
                evidence={"reasons": reasons, "rows": rows},
                detector="artifacts",
                note=f"excluded from retrieval as {klass}",
            )
        )
    return out


def classification_map(findings: list[Finding]) -> dict[str, str]:
    """Collapse the findings into {table: class} for the semantic model."""
    out: dict[str, str] = {}
    for f in findings:
        if f.kind is Kind.TABLE_CLASS and f.status is not Status.REJECTED:
            out[f.target] = f.value
    return out
