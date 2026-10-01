"""Detector: mine views, procedures and functions for knowledge (PLAN.md 4.2 step 3).

Nobody wrote documentation, but somebody wrote a view. Legacy SQL routinely encodes
exactly the things the schema forgot: which columns join to which, what status code 9
means, and which rows the reporting team has quietly been excluding for a decade.

Everything harvested here is *high confidence* — it is not inference, it is code that
has been running in production. A join predicate found in a view beats one found by
statistical inclusion testing, and the enum labels found in a CASE expression beat
anything an LLM would guess.

Parsing is sqlglot on the routine body. A routine that fails to parse is skipped, not
guessed at.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from ..connectors.base import RoutineInfo
from .findings import Finding, Kind, Status
from .introspect import Schema


@dataclass
class MinedJoin:
    left_table: str
    left_columns: list[str]
    right_table: str
    right_columns: list[str]
    expression: str
    source: str
    casts: list[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (
            self.left_table.lower(), tuple(sorted(c.lower() for c in self.left_columns)),
            self.right_table.lower(), tuple(sorted(c.lower() for c in self.right_columns)),
        )


@dataclass
class MinedFilter:
    table: str
    column: str
    operator: str
    value: Any
    expression: str
    source: str


@dataclass
class MinedEnum:
    table: str
    column: str
    mapping: dict[str, str]
    source: str


@dataclass
class MinedKnowledge:
    joins: list[MinedJoin] = field(default_factory=list)
    filters: list[MinedFilter] = field(default_factory=list)
    enums: list[MinedEnum] = field(default_factory=list)
    parsed: int = 0
    failed: list[str] = field(default_factory=list)


def _resolve_aliases(tree: exp.Expression) -> dict[str, str]:
    """alias -> real table name, for every table reference in the statement."""
    out: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        out[name.lower()] = name
        alias = table.alias
        if alias:
            out[alias.lower()] = name
    return out


def _sole_table(aliases: dict[str, str]) -> str:
    """The one real table a statement reads, if there is exactly one.

    Single-table views leave their columns unqualified, and those views carry the
    soft-delete rules we most want. With one table in scope the reference is
    unambiguous, so resolve it rather than discarding the finding.
    """
    real = {name for name in aliases.values()}
    return next(iter(real)) if len(real) == 1 else ""


def _column_ref(
    node: exp.Expression, aliases: dict[str, str], default_table: str = ""
) -> tuple[str, str] | None:
    """Unwrap casts and functions down to the (table, column) a comparison touches."""
    casts: list[str] = []
    current = node
    # CAST(t.CUST_NO AS INTEGER) -> t.CUST_NO, remembering that a cast was applied.
    while isinstance(current, (exp.Cast, exp.TryCast, exp.Paren)) or (
        isinstance(current, exp.Func) and len(list(current.find_all(exp.Column))) == 1
    ):
        if isinstance(current, (exp.Cast, exp.TryCast)):
            casts.append(current.to.sql() if current.to else "cast")
        inner = current.this
        if inner is None or inner is current:
            break
        current = inner

    if not isinstance(current, exp.Column):
        found = list(node.find_all(exp.Column))
        if len(found) != 1:
            return None
        current = found[0]

    table = current.table
    resolved = aliases.get(table.lower(), table) if table else default_table
    return (resolved, current.name) if current.name else None


def _casts_in(node: exp.Expression) -> list[str]:
    return [c.to.sql() for c in node.find_all(exp.Cast) if c.to]


def mine_joins(tree: exp.Expression, source: str) -> list[MinedJoin]:
    aliases = _resolve_aliases(tree)
    out: list[MinedJoin] = []

    def harvest(condition: exp.Expression) -> None:
        for eq in condition.find_all(exp.EQ):
            left = _column_ref(eq.this, aliases)
            right = _column_ref(eq.expression, aliases)
            if not left or not right:
                continue
            if not left[0] or not right[0] or left[0].lower() == right[0].lower():
                continue  # a self-comparison or an unqualified column tells us nothing
            out.append(
                MinedJoin(
                    left_table=left[0], left_columns=[left[1]],
                    right_table=right[0], right_columns=[right[1]],
                    expression=eq.sql(), source=source,
                    casts=_casts_in(eq),
                )
            )

    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            harvest(on)
    # Old-style comma joins put the predicate in the WHERE clause.
    for where in tree.find_all(exp.Where):
        harvest(where)

    # Collapse multi-column joins between the same table pair into one edge.
    grouped: dict[tuple[str, str], list[MinedJoin]] = defaultdict(list)
    for join in out:
        grouped[(join.left_table.lower(), join.right_table.lower())].append(join)
    merged: list[MinedJoin] = []
    for members in grouped.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        first = members[0]
        merged.append(
            MinedJoin(
                left_table=first.left_table,
                left_columns=[m.left_columns[0] for m in members],
                right_table=first.right_table,
                right_columns=[m.right_columns[0] for m in members],
                expression=" AND ".join(m.expression for m in members),
                source=first.source,
                casts=[c for m in members for c in m.casts],
            )
        )
    return merged


def mine_filters(tree: exp.Expression, source: str) -> list[MinedFilter]:
    """Constant predicates in a WHERE clause.

    These are the ones that matter: `WHERE TRANS_TYP_CD = 'I'` is a god-table
    discriminator and `WHERE DEL_FLG <> 'Y'` is a soft-delete rule, and both were
    written by somebody who knew.
    """
    aliases = _resolve_aliases(tree)
    default_table = _sole_table(aliases)
    out: list[MinedFilter] = []
    operators = {
        exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=",
        exp.LT: "<", exp.LTE: "<=", exp.Is: "IS", exp.In: "IN", exp.Like: "LIKE",
    }
    for where in tree.find_all(exp.Where):
        for node_type, operator in operators.items():
            for node in where.find_all(node_type):
                ref = _column_ref(node.this, aliases, default_table)
                if ref is None or not ref[0]:
                    continue
                right = node.args.get("expression") or node.args.get("expressions")
                if isinstance(right, list):
                    values: Any = [v.name for v in right if isinstance(v, exp.Literal)]
                    if not values:
                        continue
                elif isinstance(right, exp.Literal):
                    values = right.name
                elif isinstance(right, exp.Null):
                    values = None
                else:
                    continue
                out.append(
                    MinedFilter(
                        table=ref[0], column=ref[1], operator=operator,
                        value=values, expression=node.sql(), source=source,
                    )
                )
    return out


def mine_enums(tree: exp.Expression, source: str) -> list[MinedEnum]:
    """CASE expressions that map a code column onto labels."""
    aliases = _resolve_aliases(tree)
    default_table = _sole_table(aliases)
    out: list[MinedEnum] = []
    for case in tree.find_all(exp.Case):
        subject = case.this
        mapping: dict[str, str] = {}
        column: tuple[str, str] | None = None

        if subject is not None:
            # CASE col WHEN 1 THEN 'x'
            column = _column_ref(subject, aliases, default_table)
            for when in case.args.get("ifs", []):
                condition, then = when.this, when.args.get("true")
                if isinstance(condition, exp.Literal) and isinstance(then, exp.Literal):
                    mapping[str(condition.name)] = str(then.name)
        else:
            # CASE WHEN col = 1 THEN 'x'
            for when in case.args.get("ifs", []):
                condition, then = when.this, when.args.get("true")
                if not isinstance(condition, exp.EQ) or not isinstance(then, exp.Literal):
                    continue
                ref = _column_ref(condition.this, aliases, default_table)
                literal = condition.expression
                if ref is None or not isinstance(literal, exp.Literal):
                    continue
                if column is None:
                    column = ref
                elif column != ref:
                    mapping.clear()
                    break
                mapping[str(literal.name)] = str(then.name)

        if column and column[0] and len(mapping) >= 2:
            out.append(
                MinedEnum(table=column[0], column=column[1], mapping=mapping, source=source)
            )
    return out


def _split_statements(definition: str) -> list[str]:
    """Pull the SELECT bodies out of a routine.

    Stored procedures contain control flow sqlglot will not parse as one statement, so
    the SELECTs are extracted individually and parsed on their own.
    """
    text = re.sub(r"--[^\n]*", " ", definition)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    # A CREATE VIEW body is everything after the first AS at statement level.
    match = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b.*?\bAS\b", text, re.I | re.DOTALL)
    if match:
        return [text[match.end():]]
    statements = re.split(r";\s*", text)
    return [s for s in statements if re.search(r"\bSELECT\b", s, re.I)]


def mine_routine(routine: RoutineInfo, dialect: str) -> MinedKnowledge:
    out = MinedKnowledge()
    source = f"{routine.kind} {routine.name}"
    for statement in _split_statements(routine.definition):
        try:
            tree = sqlglot.parse_one(statement, read=dialect)
        except Exception:
            out.failed.append(source)
            continue
        if tree is None:
            continue
        out.parsed += 1
        out.joins.extend(mine_joins(tree, source))
        out.filters.extend(mine_filters(tree, source))
        out.enums.extend(mine_enums(tree, source))
    return out


def mine(schema: Schema) -> MinedKnowledge:
    """Harvest every routine in the schema."""
    combined = MinedKnowledge()
    for routine in schema.routines:
        if not routine.definition:
            continue
        result = mine_routine(routine, schema.dialect)
        combined.joins.extend(result.joins)
        combined.filters.extend(result.filters)
        combined.enums.extend(result.enums)
        combined.parsed += result.parsed
        combined.failed.extend(result.failed)
    return combined


def detect(schema: Schema) -> tuple[list[Finding], MinedKnowledge]:
    """Emit findings for everything mined, and return the raw knowledge for reuse.

    `joins.py`, `enums.py` and `softdelete.py` all take the `MinedKnowledge` as a prior,
    so it is returned alongside rather than only being rendered into findings.
    """
    knowledge = mine(schema)
    out: list[Finding] = []
    seen: set[tuple] = set()

    for join in knowledge.joins:
        key = join.key()
        if key in seen:
            continue
        seen.add(key)
        if not schema.get(join.left_table) or not schema.get(join.right_table):
            continue
        out.append(
            Finding(
                kind=Kind.JOIN,
                target=join.left_table,
                value={
                    "from_table": join.left_table,
                    "from_columns": join.left_columns,
                    "to_table": join.right_table,
                    "to_columns": join.right_columns,
                    "expression": join.expression,
                    "casts": join.casts,
                },
                confidence=0.96,
                evidence={
                    "source": join.source,
                    "reason": "this join is written in shipped SQL, not inferred",
                    "expression": join.expression,
                },
                detector="procmine",
                status=Status.ACCEPTED,
            )
        )

    for mined in knowledge.enums:
        if not schema.get(mined.table):
            continue
        out.append(
            Finding(
                kind=Kind.ENUM,
                target=f"{mined.table}.{mined.column}",
                value=mined.mapping,
                confidence=0.94,
                evidence={
                    "source": mined.source,
                    "reason": "decoded by a CASE expression in shipped SQL",
                    "mapping": mined.mapping,
                },
                detector="procmine",
                status=Status.ACCEPTED,
            )
        )

    # A constant predicate that appears in several routines is a house rule.
    by_predicate: dict[tuple, list[MinedFilter]] = defaultdict(list)
    for filt in knowledge.filters:
        by_predicate[(filt.table.lower(), filt.column.lower(), filt.operator,
                      str(filt.value))].append(filt)
    for (table, column, operator, value), members in by_predicate.items():
        resolved = schema.get(table)
        if resolved is None:
            continue
        out.append(
            Finding(
                kind=Kind.QUALITY,
                target=f"{resolved.qualified}.{column}",
                value={"operator": operator, "value": value, "kind": "routine_filter"},
                confidence=min(0.95, 0.6 + 0.1 * len(members)),
                evidence={
                    "sources": sorted({m.source for m in members}),
                    "occurrences": len(members),
                    "expression": members[0].expression,
                    "reason": "a constant predicate applied by shipped SQL",
                },
                detector="procmine",
            )
        )
    return out, knowledge
