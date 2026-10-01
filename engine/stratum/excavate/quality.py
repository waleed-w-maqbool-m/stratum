"""Detector: string quality (anti-pattern #14).

CHAR padding, `'N/A'` where NULL was meant, `New York` next to `NEW YORK`, and a
country column holding `US`, `USA`, `U.S.A.` and `United States` for the same place.

Each of these quietly breaks GROUP BY and equality. Counting customers by country
returns four rows for the United States, and the answer is wrong in a way that looks
right. The output is a canonicalisation expression per column plus, where the values
cluster, a synonym map the value linker uses to turn "new york" in a question into
whatever is actually stored.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .findings import Finding, Kind, Status
from .introspect import Schema
from .patterns import NULL_MARKERS
from .profile import ColumnProfile, ProfileSet

# How close two values have to be before they are treated as the same thing.
SYNONYM_THRESHOLD = 82


@dataclass
class QualityIssue:
    kind: str
    expression: str
    reason: str
    confidence: float
    detail: dict


def _null_marker_issue(profile: ColumnProfile) -> QualityIssue | None:
    shape = profile.value_shape
    if shape is None or shape.null_marker_fraction < 0.01:
        return None
    markers = [m for m in shape.null_markers_seen if m.strip()] or [""]
    conditions = " , ".join(f"'{m}'" for m in markers)
    expression = (
        f"CASE WHEN TRIM(COALESCE({profile.column},'')) = '' "
        f"OR UPPER(TRIM({profile.column})) IN ({conditions.upper()}) "
        f"THEN NULL ELSE {profile.column} END"
    )
    return QualityIssue(
        kind="null_markers",
        expression=expression,
        reason=(
            f"{shape.null_marker_fraction:.0%} of values are placeholders for NULL "
            f"({', '.join(repr(m) for m in shape.null_markers_seen[:5])}); counting "
            "non-null values without this returns too many"
        ),
        confidence=0.9,
        detail={"markers": shape.null_markers_seen,
                "fraction": shape.null_marker_fraction},
    )


def _padding_issue(profile: ColumnProfile) -> QualityIssue | None:
    shape = profile.value_shape
    if shape is None or shape.has_leading_trailing_space < 0.2:
        return None
    return QualityIssue(
        kind="whitespace",
        expression=f"RTRIM(LTRIM({profile.column}))",
        reason=(
            f"{shape.has_leading_trailing_space:.0%} of values carry leading or "
            "trailing whitespace"
            + (f"; the column is padded to {shape.padded_to} characters"
               if shape.padded_to else "")
        ),
        confidence=0.92,
        detail={"padded_to": shape.padded_to,
                "fraction": shape.has_leading_trailing_space},
    )


def _case_issue(profile: ColumnProfile) -> QualityIssue | None:
    shape = profile.value_shape
    if shape is None or shape.case_variants == 0:
        return None
    if profile.distinct_count == 0:
        return None
    ratio = shape.case_variants / profile.distinct_count
    if ratio < 0.02:
        return None
    return QualityIssue(
        kind="case_variants",
        expression=f"UPPER(RTRIM(LTRIM({profile.column})))",
        reason=(
            f"{shape.case_variants} values appear in more than one casing, so grouping "
            "on this column splits the same value across several rows"
        ),
        confidence=0.85,
        detail={"variants": shape.case_variants, "distinct": profile.distinct_count},
    )


def _same_spelling(left: str, right: str) -> bool:
    """Are these two strings the same value written differently?

    Only on evidence that does not happen by accident. `partial_ratio` is deliberately
    not used: it scores "us" against "australia" at 100, because containment is not
    similarity, and that single mistake files Australia under the United States.
    """
    from rapidfuzz import fuzz

    a, b = left.casefold().strip(), right.casefold().strip()
    if a == b:
        return True

    # Punctuation and spacing only: 'U.S.A.' and 'USA', 'New York' and 'new-york'.
    stripped_a = "".join(c for c in a if c.isalnum())
    stripped_b = "".join(c for c in b if c.isalnum())
    if stripped_a and stripped_a == stripped_b:
        return True

    # No prefix rule. 'US' of 'USA' looks like one, but so does 'NS' of 'NSW' — Nova
    # Scotia and New South Wales — and 'CA' is California as often as Canada. Whether
    # an abbreviation names the same thing is world knowledge, and the value
    # enrichment step exists to supply it; a string rule that guesses merges places.

    # Different numbers are different values: 'Room 1' is not 'Room 2', and 'FORMER1'
    # is not 'Former0' however similar the letters around them.
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False

    # A typo or a plural, judged word by word. Whole-string similarity rewards shared
    # structure rather than shared meaning: 'Purchased AAC audio file' and 'Protected
    # AAC audio file' score high on their common tail although the word that differs is
    # the one that says what the value is.
    words_a, words_b = a.split(), b.split()
    if len(words_a) != len(words_b):
        return False
    return all(
        fuzz.ratio(x, y) >= SYNONYM_THRESHOLD for x, y in zip(words_a, words_b, strict=True)
    )


# A value containing any of these is an identifier, a code, a date, an address or a
# document — something whose near-duplicates are *different* values. '0000000004' and
# '0000000002' differ by one character, and are two customers.
_NOT_A_WORD = re.compile(r"[\d@<>{}|/\\:]")


def _is_categorical_text(values: list[str]) -> bool:
    """Do these values read as words, where spelling variation is possible at all?"""
    wordlike = [v for v in values if not _NOT_A_WORD.search(v)]
    return len(wordlike) >= 0.8 * len(values)


def _synonym_clusters(profile: ColumnProfile) -> dict[str, list[str]] | None:
    """Group stored values that are spellings of the same thing.

    Conservative on purpose. It will not merge "United States" with "USA": that is world
    knowledge, not string similarity, and a clusterer that guesses it will also merge
    things that merely look alike. Those go to enrichment to propose and a human to
    confirm.
    """
    if profile.numeric or profile.temporal:
        return None
    if profile.distinct_count > 60 or profile.distinct_count < 2:
        return None
    values = [str(v).strip() for v, _ in profile.top_values if str(v).strip()]
    values = [v for v in values if v.casefold() not in NULL_MARKERS]
    if len(values) < 3:
        return None
    if not _is_categorical_text(values):
        return None                      # ids, codes, dates, emails: near is not same

    counts = {str(v).strip(): c for v, c in profile.top_values}
    clusters: dict[str, list[str]] = {}
    assigned: set[str] = set()
    # The most frequent spelling becomes the canonical one: it is the form the rest of
    # the business already reads in its reports.
    for value in sorted(values, key=lambda v: (-counts.get(v, 0), v)):
        if value in assigned:
            continue
        members = [value]
        assigned.add(value)
        for other in values:
            if other in assigned:
                continue
            if _same_spelling(value, other):
                members.append(other)
                assigned.add(other)
        if len(members) > 1:
            clusters[value] = members
    return clusters or None


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
        table_profile = profiles.get(table.qualified)
        if table_profile is None:
            continue

        for column in table.columns:
            profile = table_profile.column(column.name)
            if profile is None or profile.sampled_rows == 0 or profile.numeric:
                continue
            if profile.temporal:
                continue                 # a native date has no padding or spelling

            issues = [
                issue for issue in (
                    _null_marker_issue(profile),
                    _padding_issue(profile),
                    _case_issue(profile),
                )
                if issue is not None
            ]
            for issue in issues:
                out.append(
                    Finding(
                        kind=Kind.QUALITY,
                        target=f"{table.qualified}.{column.name}",
                        value={
                            "table": table.qualified,
                            "column": column.name,
                            "issue": issue.kind,
                            "expression": issue.expression,
                        },
                        confidence=issue.confidence,
                        evidence={"reason": issue.reason, **issue.detail},
                        detector="quality",
                        status=Status.ACCEPTED,
                    )
                )

            clusters = _synonym_clusters(profile)
            if clusters:
                out.append(
                    Finding(
                        kind=Kind.QUALITY,
                        target=f"{table.qualified}.{column.name}",
                        value={
                            "table": table.qualified,
                            "column": column.name,
                            "issue": "synonyms",
                            "clusters": clusters,
                        },
                        confidence=0.65,
                        evidence={
                            "reason": (
                                f"{len(clusters)} groups of values look like different "
                                "spellings of the same thing; grouping on this column "
                                "splits them apart"
                            ),
                            "examples": {k: v for k, v in list(clusters.items())[:5]},
                            "canonical_expression": _canonical_case(
                                column.name, clusters
                            ),
                        },
                        detector="quality",
                        status=Status.HYPOTHESIS,
                        note=(
                            "applied when grouping or filtering on this column, so the "
                            "same value does not appear as several rows"
                        ),
                    )
                )
    return out


def _canonical_case(column: str, clusters: dict[str, list[str]]) -> str:
    """A CASE that folds every known spelling onto its canonical form."""
    whens: list[str] = []
    for canonical, members in clusters.items():
        for member in members:
            if member == canonical:
                continue
            literal = member.replace("'", "''").upper()
            target = canonical.replace("'", "''")
            whens.append(f"WHEN '{literal}' THEN '{target}'")
    if not whens:
        return ""
    return f"CASE UPPER(TRIM({column})) {' '.join(whens)} ELSE {column} END"


def canonical_expressions(findings: list[Finding]) -> dict[str, list[str]]:
    """{table.column: [expression, ...]} in the order they should be applied."""
    out: dict[str, list[str]] = defaultdict(list)
    priority = {"whitespace": 0, "null_markers": 1, "case_variants": 2}
    for finding in findings:
        if finding.kind is not Kind.QUALITY or finding.status is Status.REJECTED:
            continue
        expression = finding.value.get("expression")
        if expression:
            out[finding.target].append(expression)
    for key, expressions in out.items():
        out[key] = sorted(expressions, key=lambda e: priority.get(e[:4], 9))
    return dict(out)
