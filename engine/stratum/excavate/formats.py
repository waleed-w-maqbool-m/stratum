"""Detector: formats embedded inside columns (anti-pattern #8).

A whole address as XML in an `NVARCHAR`. Preferences as a JSON string. Attributes as
`color=Red|size=M`. The fields inside are what people ask questions about — "customers
in Berlin" needs the city out of the XML — so each discovered path becomes a logical
column with an extraction expression.

The extraction has to be written in what the *connection's profile* allows, which is
the interesting part: SQL Server 2008 has `.value()` for XML but no JSON functions at
all, and SQLite has JSON functions but nothing for XML. Where the dialect genuinely
cannot express it, the field is marked `python_extract` and the fallback ladder (R3)
pulls a bounded result set out and finishes the work in DuckDB.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..connectors.profiles import CapabilityProfile
from .findings import Finding, Kind, Status
from .introspect import Schema
from .naming import expand
from .profile import ProfileSet

# Below this share of values parsing cleanly, it is not a format, it is a coincidence.
MIN_PARSE_RATE = 0.6


@dataclass
class Extraction:
    path: str
    logical_name: str
    expression: str          # SQL, or "" when the dialect cannot do it
    python_extract: bool
    example: str
    opaque_name: bool = False    # the path itself is cryptic; needs enrichment


def xml_expression(column: str, path: str, profile: CapabilityProfile) -> tuple[str, bool]:
    """An XML path, in whatever the dialect offers."""
    if profile.xml_value:
        # SQL Server: CAST to xml then .value(). Postgres has xpath().
        if profile.sqlglot_dialect == "tsql":
            xpath = "/" + path if not path.startswith("/") else path
            return (
                f"CAST({column} AS XML).value('({xpath})[1]', 'varchar(200)')",
                False,
            )
        if profile.sqlglot_dialect == "postgres":
            xpath = "/" + path if not path.startswith("/") else path
            return f"(xpath('{xpath}/text()', {column}::xml))[1]::text", False

    # No XML support: recover the element with string functions. Legacy address blobs
    # are flat enough that this is exact, and it is the only option on SQLite.
    tag = path.rsplit("/", 1)[-1]
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    expression = (
        f"CASE WHEN INSTR({column}, '{open_tag}') > 0 THEN "
        f"SUBSTR({column}, INSTR({column}, '{open_tag}') + {len(open_tag)}, "
        f"INSTR({column}, '{close_tag}') - INSTR({column}, '{open_tag}') "
        f"- {len(open_tag)}) END"
    )
    return expression, False


def json_expression(column: str, path: str, profile: CapabilityProfile) -> tuple[str, bool]:
    if not profile.json_fns:
        # SQL Server 2008 has no JSON at all. Mark for the Python fallback rather than
        # emitting string surgery that will break on the first escaped quote.
        return "", True
    if profile.sqlglot_dialect == "sqlite":
        return f"JSON_EXTRACT({column}, '$.{path}')", False
    if profile.sqlglot_dialect == "postgres":
        parts = path.split(".")
        accessor = "".join(f"->'{p}'" for p in parts[:-1]) + f"->>'{parts[-1]}'"
        return f"({column}::jsonb{accessor})", False
    return f"JSON_VALUE({column}, '$.{path}')", False


def kv_expression(
    column: str, key: str, pair_sep: str, assign: str, profile: CapabilityProfile
) -> tuple[str, bool]:
    """Pull one key out of `a=1|b=2` with string functions.

    Works everywhere: find `key=`, then take everything up to the next separator.
    """
    needle = f"{key}{assign}"
    start = f"INSTR({column}, '{needle}') + {len(needle)}"
    tail = f"SUBSTR({column}, {start})"
    expression = (
        f"CASE WHEN INSTR({column}, '{needle}') > 0 THEN "
        f"CASE WHEN INSTR({tail}, '{pair_sep}') > 0 "
        f"THEN SUBSTR({tail}, 1, INSTR({tail}, '{pair_sep}') - 1) "
        f"ELSE {tail} END END"
    )
    if profile.sqlglot_dialect == "tsql":
        expression = expression.replace("INSTR(", "CHARINDEX_SWAP(")
        # T-SQL's CHARINDEX takes its arguments the other way round; rewrite properly.
        expression = _tsql_charindex(column, needle, pair_sep)
    return expression, False


def _tsql_charindex(column: str, needle: str, pair_sep: str) -> str:
    start = f"CHARINDEX('{needle}', {column}) + {len(needle)}"
    tail = f"SUBSTRING({column}, {start}, 8000)"
    return (
        f"CASE WHEN CHARINDEX('{needle}', {column}) > 0 THEN "
        f"CASE WHEN CHARINDEX('{pair_sep}', {tail}) > 0 "
        f"THEN SUBSTRING({tail}, 1, CHARINDEX('{pair_sep}', {tail}) - 1) "
        f"ELSE {tail} END END"
    )


def detect(
    schema: Schema,
    profiles: ProfileSet,
    capability: CapabilityProfile,
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
            if profile is None or not profile.structure_format:
                continue
            shape = profile.value_shape
            if shape is None:
                continue
            fmt = profile.structure_format
            parse_rate = max(
                shape.signatures.get("xml", 0),
                shape.signatures.get("json_object", 0),
                shape.signatures.get("kv_pipe", 0),
                shape.signatures.get("kv_semicolon", 0),
            )
            if parse_rate < MIN_PARSE_RATE or not profile.structure_paths:
                continue

            extractions: list[Extraction] = []
            for path, example in sorted(profile.structure_paths.items()):
                if fmt == "xml":
                    expression, python_only = xml_expression(column.name, path, capability)
                elif fmt == "json":
                    expression, python_only = json_expression(column.name, path, capability)
                else:
                    pair_sep = "|" if shape.signatures.get("kv_pipe", 0) > 0 else ";"
                    expression, python_only = kv_expression(
                        column.name, path, pair_sep, "=", capability
                    )
                tag = path.rsplit("/", 1)[-1]
                expanded = expand(tag)
                # An XML blob's tags are as cryptic as the schema around it: <c> is a
                # city and <cy> a country. A one-letter logical column is useless, so
                # fall back to naming the field after the column it lives in and let
                # enrichment give it a real name from the example values.
                if expanded.is_opaque or len(tag) <= 2:
                    logical = f"{expand(column.name).logical}_{tag.lower()}"
                    opaque = True
                else:
                    logical = expanded.logical or tag
                    opaque = False
                extractions.append(
                    Extraction(
                        path=path, logical_name=logical, expression=expression,
                        python_extract=python_only, example=str(example)[:60],
                        opaque_name=opaque,
                    )
                )

            out.append(
                Finding(
                    kind=Kind.EMBEDDED_FORMAT,
                    target=f"{table.qualified}.{column.name}",
                    value={
                        "table": table.qualified,
                        "column": column.name,
                        "format": fmt,
                        "fields": [
                            {
                                "path": e.path,
                                "logical_name": e.logical_name,
                                "expression": e.expression,
                                "python_extract": e.python_extract,
                                "opaque_name": e.opaque_name,
                                "example": e.example,
                            }
                            for e in extractions
                        ],
                    },
                    confidence=round(min(0.95, 0.6 + parse_rate * 0.35), 3),
                    evidence={
                        "parse_rate": round(parse_rate, 3),
                        "paths_found": len(extractions),
                        "examples": {e.path: e.example for e in extractions[:6]},
                        "dialect_limitation": (
                            f"{capability.name} cannot query {fmt} natively; these "
                            "fields are extracted outside the database"
                            if any(e.python_extract for e in extractions) else ""
                        ),
                    },
                    detector="formats",
                    status=Status.ACCEPTED if parse_rate > 0.95 else Status.HYPOTHESIS,
                )
            )
    return out
