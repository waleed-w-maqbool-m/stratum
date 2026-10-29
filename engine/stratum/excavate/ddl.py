"""The statements that would close a gap, written for the connection's dialect.

These are only ever shown. Nothing in Stratum executes them: the connection is read-only
and every user statement must be a single SELECT. They exist so that "this foreign key
should exist" comes with the exact `ALTER TABLE` a database administrator can review and
run, and so that the tool can say honestly when it cannot be added cleanly yet.
"""

from __future__ import annotations

import re


def quote(dialect: str, name: str) -> str:
    return f"[{name}]" if dialect == "mssql" else f'"{name}"'


def qualified(dialect: str, table: str) -> str:
    return ".".join(quote(dialect, part) for part in table.split("."))


def constraint_name(prefix: str, *parts: str) -> str:
    text = "_".join(p.rsplit(".", 1)[-1] for p in parts)
    return re.sub(r"[^0-9A-Za-z_]", "_", f"{prefix}_{text}")[:100]


def unique_ddl(dialect: str, table: str, columns: list[str]) -> str:
    cols = ", ".join(quote(dialect, c) for c in columns)
    name = constraint_name("UQ", table, *columns)
    return (
        f"ALTER TABLE {qualified(dialect, table)} "
        f"ADD CONSTRAINT {quote(dialect, name)} UNIQUE ({cols});"
    )


def foreign_key_ddl(
    dialect: str,
    child: str,
    child_columns: list[str],
    parent: str,
    parent_columns: list[str],
    *,
    orphans: int = 0,
    parent_has_key: bool = True,
) -> str:
    """`ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`, tolerant of existing orphans.

    With orphans the constraint cannot be validated against what is already there. SQL
    Server's `WITH NOCHECK` and Postgres's `NOT VALID` add it for new writes only, which
    is the honest option for a legacy table; the comment says why it was chosen.
    """
    if dialect == "sqlite":
        return (
            "-- SQLite cannot add a foreign key to an existing table. Rebuild "
            f"{child.rsplit('.', 1)[-1]} with "
            f"FOREIGN KEY ({', '.join(child_columns)}) REFERENCES "
            f"{parent.rsplit('.', 1)[-1]} ({', '.join(parent_columns)})."
        )

    lines: list[str] = []
    if not parent_has_key:
        lines.append(
            f"-- {parent} needs a unique key on ({', '.join(parent_columns)}) first."
        )
        lines.append(unique_ddl(dialect, parent, parent_columns))
    if orphans:
        lines.append(
            f"-- {orphans} existing value{'s have' if orphans != 1 else ' has'} no parent "
            "row, so the constraint is added without checking existing rows."
        )

    name = quote(dialect, constraint_name("FK", child, parent, *child_columns))
    cols = ", ".join(quote(dialect, c) for c in child_columns)
    refs = ", ".join(quote(dialect, c) for c in parent_columns)
    if dialect == "mssql":
        mode = "WITH NOCHECK" if orphans else "WITH CHECK"
        lines.append(
            f"ALTER TABLE {qualified(dialect, child)} {mode} ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({cols}) REFERENCES {qualified(dialect, parent)} ({refs});"
        )
    else:
        tail = " NOT VALID" if orphans else ""
        lines.append(
            f"ALTER TABLE {qualified(dialect, child)} ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({cols}) REFERENCES {qualified(dialect, parent)} ({refs}){tail};"
        )
    return "\n".join(lines)


def lookup_ddl(
    dialect: str,
    table: str,
    column: str,
    column_type: str,
    labels: dict[str, str],
    *,
    limit: int = 12,
) -> str:
    """A lookup table for a coded column, seeded with the decoded labels."""
    if dialect == "sqlite":
        return (
            f"-- Create a lookup table for {table.rsplit('.', 1)[-1]}.{column} with "
            f"{len(labels)} code(s), then reference it."
        )
    lookup = f"{table}_{column}_lookup"
    lookup_q = qualified(dialect, lookup)
    text_type = "NVARCHAR(100)" if dialect == "mssql" else "VARCHAR(100)"
    lines = [
        f"CREATE TABLE {lookup_q} (\n"
        f"  {quote(dialect, 'code')} {column_type or text_type} NOT NULL PRIMARY KEY,\n"
        f"  {quote(dialect, 'label')} {text_type} NOT NULL\n);"
    ]
    shown = list(labels.items())[:limit]

    def literal(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    rows = ",\n  ".join(f"({literal(k)}, {literal(v)})" for k, v in shown)
    suffix = f"\n-- {len(labels) - limit} more code(s) not shown." if len(labels) > limit else ""
    lines.append(f"INSERT INTO {lookup_q} ({quote(dialect, 'code')}, {quote(dialect, 'label')}) "
                 f"VALUES\n  {rows};{suffix}")
    lines.append(
        foreign_key_ddl(dialect, table, [column], lookup, ["code"], orphans=0)
    )
    return "\n".join(lines)
