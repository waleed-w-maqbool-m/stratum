"""Step 1 of excavation: what is actually in this database.

Pulls the catalog into one immutable snapshot that every later step reads. Nothing
here samples data or guesses anything — it is the ground truth the server will admit
to, which in a legacy database is a good deal less than the truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..connectors.base import (
    ColumnInfo,
    Connector,
    ForeignKeyInfo,
    IndexInfo,
    RoutineInfo,
    TableInfo,
)


@dataclass
class Table:
    info: TableInfo
    columns: list[ColumnInfo] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def schema(self) -> str:
        return self.info.schema

    @property
    def qualified(self) -> str:
        return self.info.qualified

    @property
    def row_estimate(self) -> int:
        return self.info.row_estimate

    @property
    def is_view(self) -> bool:
        return self.info.kind == "view"

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> ColumnInfo | None:
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    def indexed_columns(self) -> set[str]:
        """Leading columns of any index — the ones a filter can actually use."""
        return {ix.columns[0].lower() for ix in self.indexes if ix.columns}


@dataclass
class Schema:
    """The whole catalog, keyed for fast lookup."""

    dialect: str
    database: str
    tables: dict[str, Table] = field(default_factory=dict)   # keyed by lowercase qualified
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    routines: list[RoutineInfo] = field(default_factory=list)

    def get(self, name: str) -> Table | None:
        """Look a table up by qualified or bare name, case-insensitively."""
        key = name.lower()
        if key in self.tables:
            return self.tables[key]
        # bare name against a schema-qualified catalog
        matches = [t for k, t in self.tables.items() if k.rsplit(".", 1)[-1] == key]
        return matches[0] if len(matches) == 1 else (matches[0] if matches else None)

    @property
    def base_tables(self) -> list[Table]:
        return [t for t in self.tables.values() if not t.is_view]

    @property
    def views(self) -> list[Table]:
        return [t for t in self.tables.values() if t.is_view]

    def total_columns(self) -> int:
        return sum(len(t.columns) for t in self.tables.values())

    def summary(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "dialect": self.dialect,
            "tables": len(self.base_tables),
            "views": len(self.views),
            "columns": self.total_columns(),
            "declared_foreign_keys": len(self.foreign_keys),
            "routines": len(self.routines),
            "total_rows_estimated": sum(t.row_estimate for t in self.tables.values()),
        }


def introspect(connector: Connector) -> Schema:
    """Build the snapshot. One pass over the catalog, no data reads."""
    info = connector.server_info()
    schema = Schema(dialect=info.dialect, database=info.database)

    for table_info in connector.list_tables():
        schema.tables[table_info.qualified.lower()] = Table(info=table_info)

    for column in connector.list_columns():
        table = schema.tables.get(column.qualified_table.lower())
        if table is not None:
            table.columns.append(column)
    for table in schema.tables.values():
        table.columns.sort(key=lambda c: c.ordinal)

    for index in connector.list_indexes():
        table = schema.tables.get(index.qualified_table.lower())
        if table is None:
            continue
        table.indexes.append(index)
        if index.is_primary:
            table.primary_key = list(index.columns)
        elif index.is_unique:
            table.unique_keys.append(list(index.columns))

    schema.foreign_keys = connector.list_foreign_keys()
    schema.routines = [r for r in connector.list_routines() if r.definition]
    return schema
