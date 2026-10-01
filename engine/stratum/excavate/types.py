"""Detector: inconsistent types (anti-pattern #13).

The same fact stored three different ways. `CUST_ID` is an `INT` here and a zero-padded
`VARCHAR(10)` there. Dates live in `DATETIME`, in `CHAR(8)` as `YYYYMMDD`, and in a
`VARCHAR` as `DD/MM/YYYY`. Money is a `FLOAT`, a `DECIMAL`, and an integer count of
cents.

Output is a normalisation expression per column: the SQL that turns whatever is stored
into the logical type. The compiler applies these when it expands an entity, so the
generated SQL never has to know that `ORD_DT_TXT` is a string.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .findings import Finding, Kind, Status
from .introspect import Schema
from .naming import entity_words, expand
from .patterns import DATE_SIGNATURES
from .profile import ColumnProfile, ProfileSet

# How a stored shape maps onto a logical type and the SQL that converts it.
# The conversions are written in portable SQL; `dialect.py` rewrites them per profile.
DATE_CONVERSIONS: dict[str, str] = {
    "date_yyyymmdd": (
        "SUBSTR({col},1,4) || '-' || SUBSTR({col},5,2) || '-' || SUBSTR({col},7,2)"
    ),
    "date_dmy_slash": (
        "SUBSTR({col},7,4) || '-' || SUBSTR({col},4,2) || '-' || SUBSTR({col},1,2)"
    ),
    "date_dmy_dash": (
        "SUBSTR({col},7,4) || '-' || SUBSTR({col},4,2) || '-' || SUBSTR({col},1,2)"
    ),
    "datetime_dmy": (
        "SUBSTR({col},7,4) || '-' || SUBSTR({col},4,2) || '-' || SUBSTR({col},1,2)"
    ),
    "date_iso": "{col}",
    "datetime_iso": "SUBSTR({col},1,10)",
}

# The same conversions for a database with a real DATE type. The text-splicing forms
# above produce a string that merely *looks* like a date — fine on SQLite, where dates
# are text anyway, but on Postgres EXTRACT, DATE_TRUNC and date arithmetic all reject
# it. Blank strings become NULL first: TO_DATE('') is an error, not a NULL.
_BLANK_SAFE = "NULLIF(TRIM({col}), '')"
POSTGRES_DATE_CONVERSIONS: dict[str, str] = {
    "date_yyyymmdd": f"TO_DATE({_BLANK_SAFE}, 'YYYYMMDD')",
    "date_dmy_slash": f"TO_DATE({_BLANK_SAFE}, 'DD/MM/YYYY')",
    "date_dmy_dash": f"TO_DATE({_BLANK_SAFE}, 'DD-MM-YYYY')",
    "datetime_dmy": f"TO_DATE(SUBSTR({_BLANK_SAFE}, 1, 10), 'DD/MM/YYYY')",
    "date_iso": f"CAST({_BLANK_SAFE} AS DATE)",
    "datetime_iso": f"CAST(SUBSTR({_BLANK_SAFE}, 1, 10) AS DATE)",
}

# Digit strings that are identifiers rather than quantities. Casting a phone number to
# an integer overflows (6172235589 > 2^31) and drops the leading zero of a postal code;
# neither was ever going to be summed.
IDENTIFIER_WORDS = {"phone", "fax", "postal", "zip", "postcode", "telephone", "mobile",
                    "ssn", "isbn", "barcode", "sku"}


def _conversions(dialect: str) -> dict[str, str]:
    return POSTGRES_DATE_CONVERSIONS if dialect == "postgres" else DATE_CONVERSIONS


def _to_integer(column: str, dialect: str) -> str:
    """A cast that cannot fail on a blank or a stray 'N/A'.

    SQLite's CAST turns anything non-numeric into 0 without complaint; Postgres raises
    and takes the whole query down with it. So on Postgres, only digits are cast.
    """
    if dialect == "postgres":
        return (f"CASE WHEN TRIM({column}) ~ '^-?[0-9]+$' "
                f"THEN CAST(TRIM({column}) AS BIGINT) END")
    return f"CAST({column} AS INTEGER)"


def _to_decimal(column: str, dialect: str) -> str:
    if dialect == "postgres":
        # NUMERIC, not REAL: REAL is float4 there, and ROUND(real, 2) does not exist.
        return (f"CASE WHEN TRIM({column}) ~ '^-?[0-9]*[.]?[0-9]+$' "
                f"THEN CAST(TRIM({column}) AS NUMERIC) END")
    return f"CAST({column} AS REAL)"


@dataclass
class Normalisation:
    column: str
    stored_as: str
    logical_type: str
    expression: str
    reason: str


def normalisation_for(profile: ColumnProfile, dialect: str = "sqlite") -> Normalisation | None:
    """The expression that turns this column into its logical type, if it needs one."""
    shape = profile.value_shape
    column = profile.column
    if shape is None or profile.sampled_rows == 0:
        return None
    # A native date or timestamp is already its logical type. Its str() looks like an
    # ISO date, which is how SUBSTR(txn_ts, 1, 10) was being applied to a TIMESTAMP.
    if profile.temporal:
        return None

    declared = (profile.declared_type or "").upper()
    conversions = _conversions(dialect)

    # --- dates stored as text
    date_shapes = {s: f for s, f in shape.signatures.items() if s in DATE_SIGNATURES}
    if date_shapes:
        dominant = max(date_shapes, key=lambda s: date_shapes[s])
        if date_shapes[dominant] >= 0.9 and dominant in conversions:
            if dominant == "date_iso" and "DATE" in declared:
                return None            # already a date; nothing to do
            return Normalisation(
                column=column,
                stored_as=dominant,
                logical_type="date",
                expression=conversions[dominant].format(col=column),
                reason=(
                    f"stored as text in {dominant.replace('_', ' ')} form, not as a date"
                ),
            )
        if len(date_shapes) > 1:
            # Several formats in one column: build a CASE over the shapes present.
            branches = []
            for signature, fraction in sorted(
                date_shapes.items(), key=lambda kv: -kv[1]
            ):
                if signature not in conversions or fraction < 0.02:
                    continue
                conversion = conversions[signature].format(col=column)
                if signature == "date_yyyymmdd":
                    test = f"LENGTH({column}) = 8 AND {column} NOT LIKE '%/%'"
                elif "dmy" in signature or "mdy" in signature:
                    test = f"{column} LIKE '__/__/____%'"
                else:
                    test = f"{column} LIKE '____-__-__%'"
                branches.append(f"WHEN {test} THEN {conversion}")
            if branches:
                return Normalisation(
                    column=column,
                    stored_as="mixed date formats: " + ", ".join(sorted(date_shapes)),
                    logical_type="date",
                    expression="CASE " + " ".join(branches) + " END",
                    reason=(
                        "this column holds "
                        f"{len(date_shapes)} different date formats: "
                        + ", ".join(f"{s} ({f:.0%})" for s, f in sorted(date_shapes.items()))
                    ),
                )

    # --- numbers stored as zero-padded text
    if shape.signatures.get("zero_padded_int", 0) >= 0.9:
        return Normalisation(
            column=column,
            stored_as="zero-padded text",
            logical_type="integer",
            expression=_to_integer(column, dialect),
            reason=(
                f"every value is a zero-padded integer such as "
                f"{next((str(v) for v, _ in profile.top_values), '')!r}"
            ),
        )

    # --- fixed-width CHAR padding
    if shape.padded_to:
        return Normalisation(
            column=column,
            stored_as=f"CHAR({shape.padded_to}), space padded",
            logical_type="text",
            expression=f"RTRIM({column})",
            reason=(
                f"every value is padded to exactly {shape.padded_to} characters, so "
                "comparisons and grouping need the padding removed"
            ),
        )

    # --- a numeric column stored as text
    numeric_share = shape.signatures.get("integer", 0) + shape.signatures.get("decimal", 0)
    if numeric_share >= 0.95 and not profile.numeric:
        if set(expand(column).words) & IDENTIFIER_WORDS:
            return None                # a phone number is digits, not a number
        logical = "decimal" if shape.signatures.get("decimal", 0) > 0.1 else "integer"
        return Normalisation(
            column=column,
            stored_as="numeric text",
            logical_type=logical,
            expression=(
                _to_decimal(column, dialect) if logical == "decimal"
                else _to_integer(column, dialect)
            ),
            reason=f"{numeric_share:.0%} of values are numbers stored as text",
        )
    return None


def detect(
    schema: Schema,
    profiles: ProfileSet,
    table_classes: dict[str, str] | None = None,
) -> list[Finding]:
    classes = {k.lower(): v for k, v in (table_classes or {}).items()}
    out: list[Finding] = []

    # Columns that mean the same thing, grouped by their expanded name, so that
    # "the same fact stored three ways" can be reported as one finding.
    by_meaning: dict[str, list[tuple[str, ColumnProfile]]] = defaultdict(list)

    for table in schema.base_tables:
        if classes.get(table.qualified.lower(), "core") != "core":
            continue
        table_profile = profiles.get(table.qualified)
        if table_profile is None:
            continue

        for column in table.columns:
            profile = table_profile.column(column.name)
            if profile is None:
                continue
            normalisation = normalisation_for(profile, schema.dialect)
            if normalisation is not None:
                out.append(
                    Finding(
                        kind=Kind.TYPE_MISMATCH,
                        target=f"{table.qualified}.{column.name}",
                        value={
                            "table": table.qualified,
                            "column": column.name,
                            "declared_type": column.data_type,
                            "stored_as": normalisation.stored_as,
                            "logical_type": normalisation.logical_type,
                            "expression": normalisation.expression,
                        },
                        confidence=0.9,
                        evidence={
                            "reason": normalisation.reason,
                            "examples": [
                                str(v) for v in profile.sample_values(3)
                            ],
                            "declared_type": column.data_type,
                        },
                        detector="types",
                        status=Status.ACCEPTED,
                    )
                )

            words = entity_words(column.name) or set(expand(column.name).words)
            if words:
                by_meaning["+".join(sorted(words))].append((table.qualified, profile))

    # --- the same meaning stored under different types in different tables
    for meaning, members in by_meaning.items():
        if len(members) < 2:
            continue
        storage = {}
        for qualified, profile in members:
            shape = profile.value_shape
            key = "numeric" if profile.numeric else (
                shape.dominant if shape else "unknown"
            )
            storage.setdefault(key, []).append(f"{qualified}.{profile.column}")
        if len(storage) < 2:
            continue
        # Ignore accidental groupings of unrelated columns that share a common word.
        if len(members) > 8:
            continue
        out.append(
            Finding(
                kind=Kind.TYPE_MISMATCH,
                target=members[0][0],
                value={
                    "meaning": meaning.replace("+", " "),
                    "storage_forms": storage,
                    "kind": "cross_table",
                },
                confidence=0.7,
                evidence={
                    "reason": (
                        f"'{meaning.replace('+', ' ')}' is stored in "
                        f"{len(storage)} different forms across the schema"
                    ),
                    "forms": storage,
                },
                detector="types",
                status=Status.HYPOTHESIS,
                note="joins across these columns need a cast on one side",
            )
        )
    return out
