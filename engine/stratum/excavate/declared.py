"""Detector: relationships the database declares.

A catalog foreign key is the strongest evidence a relationship can have, and the only
kind nobody has to interpret. It is read here as a finding like any other so the rest of
the pipeline treats all relationships alike, but it is taken as given, never re-derived.

`joins.detect` folds these in, checks them against the data once, and re-emits them with
the measurements attached. That check matters: a constraint that is disabled or was added
`WITH NOCHECK` routinely has orphan rows in it, and a legacy database is where that is
most likely.
"""

from __future__ import annotations

from ..origin import Origin
from .findings import Finding, Kind, Status
from .introspect import Schema, Table

# A constraint the engine is not enforcing is still a declared relationship, but it is
# weaker evidence than one it checks on every write.
UNENFORCED_CONFIDENCE = 0.97


def _table(schema: Schema, schema_name: str, name: str) -> Table | None:
    if schema_name:
        found = schema.tables.get(f"{schema_name}.{name}".lower())
        if found is not None:
            return found
    return schema.get(name)


def expression(from_columns: list[str], to_columns: list[str]) -> str:
    return " AND ".join(
        f"child.{c} = parent.{p}" for c, p in zip(from_columns, to_columns, strict=False)
    )


def detect(schema: Schema) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple] = set()
    for fk in schema.foreign_keys:
        child = _table(schema, fk.schema, fk.table)
        parent = _table(schema, fk.ref_schema, fk.ref_table)
        if child is None or parent is None or child.is_view:
            continue
        if not fk.columns or len(fk.columns) != len(fk.ref_columns):
            continue
        key = (
            child.qualified.lower(), tuple(c.lower() for c in fk.columns),
            parent.qualified.lower(), tuple(c.lower() for c in fk.ref_columns),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Finding(
                kind=Kind.JOIN,
                target=child.qualified,
                value={
                    "from_table": child.qualified,
                    "from_columns": list(fk.columns),
                    "to_table": parent.qualified,
                    "to_columns": list(fk.ref_columns),
                    "expression": expression(fk.columns, fk.ref_columns),
                    "needs_cast": False,
                },
                confidence=1.0 if fk.enforced is not False else UNENFORCED_CONFIDENCE,
                evidence={
                    "origin": Origin.DECLARED.value,
                    "source": "declared foreign key",
                    "fk_name": fk.name,
                    "enforced": fk.enforced,
                    "weakness": fk.weakness,
                },
                detector="declared",
                status=Status.ACCEPTED,
            )
        )
    return out
