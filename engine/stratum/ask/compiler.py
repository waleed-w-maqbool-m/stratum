"""Logical SQL to physical SQL.

This is the distinctive bet of the whole system (PLAN.md §1). The language model writes
ordinary SQL against a clean, *virtual* schema — `customer.first_name`,
`order.total_amount` — and this module rewrites it deterministically into the ugly
physical reality. Legacy mess is handled once, in reviewed code and YAML, rather than
re-guessed by a model on every question.

The mechanism is deliberately boring: each entity becomes a derived table that projects
its logical columns and applies its own filters.

    customer  ->  (SELECT RTRIM(CFNM1) AS first_name,
                          CAST(REP_NO AS INTEGER) AS support_rep_id
                   FROM CUST_MSTR
                   WHERE UPPER(COALESCE(DEL_FLG,'N')) <> 'Y') AS customer

Because the derived table exposes the logical names, everything above it — the SELECT
list, the WHERE, the GROUP BY, the join conditions — needs no rewriting at all. The
cast that makes a join work lives in the entity that owns the column, not in the join,
so the model never has to know it exists.

Every filter applied is recorded as an assumption, because the user is owed an
explanation of why their count came back lower than they expected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from ..connectors.profiles import CapabilityProfile
from ..semantic.model import Entity, SemanticModel
from . import dialect as dialect_module


class CompileError(Exception):
    """The logical SQL cannot be compiled against this model."""


@dataclass
class Assumption:
    """Something the compiler did that the user did not ask for and should be told."""

    kind: str                  # soft_delete | discriminator | normalisation | drift | unit
    entity: str
    text: str
    expr: str = ""
    rows_excluded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity": self.entity,
            "text": self.text,
            "expr": self.expr,
            "rows_excluded": self.rows_excluded,
        }


@dataclass
class CompiledQuery:
    logical_sql: str
    physical_sql: str
    entities_used: list[str] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    downgrades: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_sql": self.logical_sql,
            "physical_sql": self.physical_sql,
            "entities": self.entities_used,
            "assumptions": [a.to_dict() for a in self.assumptions],
            "warnings": self.warnings,
            "downgrades": self.downgrades,
            "unresolved": self.unresolved,
        }


# --------------------------------------------------------------------- entity expansion


def entity_projection(entity: Entity, profile: CapabilityProfile) -> list[exp.Expression]:
    """The SELECT list of an entity's derived table: one alias per logical column."""
    out: list[exp.Expression] = []
    for name, column in entity.columns.items():
        if column.status == "rejected":
            continue
        try:
            value = sqlglot.parse_one(column.expr, read=profile.sqlglot_dialect)
        except Exception as exc:
            raise CompileError(
                f"{entity.name}.{name} has an expression that does not parse: "
                f"{column.expr!r} ({exc})"
            ) from exc
        out.append(exp.alias_(value, name, quoted=False))
    return out


def entity_filters(entity: Entity) -> tuple[list[str], list[Assumption]]:
    """Every predicate the entity applies by default, with its explanation."""
    predicates: list[str] = []
    assumptions: list[Assumption] = []
    for filt in entity.active_filters():
        predicates.append(filt.expr)
        assumptions.append(
            Assumption(
                kind=filt.kind,
                entity=entity.name,
                text=filt.reason,
                expr=filt.expr,
                rows_excluded=filt.rows_excluded,
            )
        )
    return predicates, assumptions


def expand_entity(
    entity: Entity,
    profile: CapabilityProfile,
    *,
    alias: str | None = None,
    pushdown: list[exp.Expression] | None = None,
) -> tuple[exp.Subquery, list[Assumption]]:
    """Build the derived table that stands in for one logical entity."""
    projection = entity_projection(entity, profile)
    if not projection:
        raise CompileError(f"entity {entity.name} has no usable columns")

    select = exp.Select(expressions=projection).from_(
        exp.to_table(entity.source, dialect=profile.sqlglot_dialect)
    )

    predicates, assumptions = entity_filters(entity)
    for predicate in predicates:
        select = select.where(predicate, dialect=profile.sqlglot_dialect)
    for predicate in pushdown or []:
        translated = _to_physical(predicate, entity, profile)
        if translated is not None:
            select = select.where(translated)

    subquery = exp.Subquery(
        this=select,
        alias=exp.TableAlias(this=exp.to_identifier(alias or entity.name)),
    )
    return subquery, assumptions


# ------------------------------------------------------------------------- compilation


def select_from(tree: exp.Expression) -> exp.From | None:
    """The FROM clause of a SELECT, whatever sqlglot calls the argument this version.

    sqlglot has moved this between `from` and `from_`. Reading it by argument name is
    what made the fan-out probe silently unbuildable, so it is read structurally here.
    """
    if not isinstance(tree, exp.Select):
        return None
    return tree.args.get("from_") or tree.args.get("from") or tree.find(exp.From)


def set_select_from(tree: exp.Select, from_clause: exp.From) -> None:
    """Set the FROM clause under whichever argument name this sqlglot uses."""
    key = "from_" if "from_" in type(tree).arg_types else "from"
    tree.set(key, from_clause)


def compile_logical(
    logical_sql: str,
    model: SemanticModel,
    profile: CapabilityProfile,
    *,
    push_predicates: bool = True,
) -> CompiledQuery:
    """Rewrite logical SQL into physical SQL for this connection's profile.

    The logical SQL is parsed as ANSI rather than as the target dialect. That is the
    point of having a logical layer: the generator writes ordinary SQL, and sqlglot
    translates the standard spellings into whatever this connection speaks. Parsing it
    as the target dialect instead let `EXTRACT(YEAR FROM ...)` pass straight through to
    a SQLite database that has no such function.
    """
    try:
        tree = sqlglot.parse_one(logical_sql, read=None)
    except Exception as exc:
        raise CompileError(f"logical SQL does not parse: {exc}") from exc
    if tree is None:
        raise CompileError("logical SQL is empty")

    # Refuse anything that is not a read before rewriting it. The executor checks this
    # too, but a compiler that will happily expand the entities inside a DELETE is one
    # bug away from emitting one, and defence in depth is cheap here.
    if not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)):
        raise CompileError(
            f"only SELECT can be compiled; this is a {type(tree).__name__.upper()}"
        )

    result = CompiledQuery(logical_sql=logical_sql, physical_sql="")

    # Names the query defines for itself. A CTE is not an entity, and treating it as
    # one rejected every query the "decompose" strategy produces — which is the strategy
    # the generator is told to use when the grain is unclear.
    local_names = {
        cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias
    }

    # --- find every entity reference and confirm it exists
    references: list[exp.Table] = []
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in local_names:
            continue
        entity = model.entity(name)
        if entity is None:
            # A physical table name is a mistake the repair loop should see, not
            # something to silently allow through.
            raise CompileError(
                f"unknown entity {name!r}. Available: "
                + ", ".join(sorted(model.entities)[:25])
            )
        references.append(table)

    if not references:
        raise CompileError("the query does not reference any entity")

    # --- case-insensitive comparisons, while aliases still name entities
    _fold_case(tree, model)

    # --- optional predicate pushdown into each entity's derived table
    pushdown: dict[str, list[exp.Expression]] = {}
    if push_predicates:
        pushdown = _collect_pushdown(tree, model)

    # --- replace each reference with its expansion
    seen: list[str] = []
    for table in references:
        entity = model.entity(table.name)
        assert entity is not None
        alias = table.alias or table.name
        subquery, assumptions = expand_entity(
            entity, profile, alias=alias, pushdown=pushdown.get(alias.lower())
        )
        table.replace(subquery)
        if entity.name not in seen:
            seen.append(entity.name)
            result.assumptions.extend(assumptions)

    result.entities_used = seen

    # Translate standard date idioms while the tree is still neutral. Rendering first
    # would lose the format argument of TO_CHAR and silently produce a bare CAST.
    tree, rewritten = dialect_module.normalise_for_dialect(tree, profile)
    # Every physical name in the tree came from the catalog, spelled exactly; the ones
    # this dialect would fold to something else are quoted here, once, for all of them.
    tree = dialect_module.quote_catalog_identifiers(tree, set(model.quoted_identifiers))
    physical = tree.sql(dialect=profile.sqlglot_dialect, pretty=True)

    # --- dialect downgrade, then lint
    downgraded = dialect_module.downgrade(physical, profile)
    result.physical_sql = downgraded.sql
    result.downgrades = downgraded.applied
    if rewritten:
        result.downgrades.append("standard date functions -> this dialect's idiom")
    result.unresolved = [str(v) for v in downgraded.unresolved]

    for entity_name in seen:
        entity = model.entity(entity_name)
        if entity is None:
            continue
        for column in entity.columns.values():
            if column.status == "hypothesis" and column.note:
                result.warnings.append(
                    f"{entity_name}.{column.name} uses an unreviewed mapping: {column.note}"
                )
    return result


def _nullable_aliases(tree: exp.Expression) -> set[str]:
    """Aliases on the side of an outer join that can be filled with NULLs.

    A predicate in WHERE on one of these runs *after* the join and sees the NULLs the join
    produced. Moved inside the table instead, it filters before the join and the NULLs
    never arise: `WHERE b.id IS NULL` pushed into `b` empties `b`, so every row of the left
    side looks unmatched. These are the aliases a predicate must never be moved into.
    """
    nullable: set[str] = set()
    if not isinstance(tree, exp.Select):
        return nullable
    from_clause = select_from(tree)
    before = [from_clause.this.alias_or_name.lower()] if from_clause is not None else []
    for join in tree.args.get("joins") or []:
        side = (join.side or "").upper()
        name = join.this.alias_or_name.lower()
        if side in ("LEFT", "FULL"):
            nullable.add(name)
        if side in ("RIGHT", "FULL"):
            nullable.update(before)
        before.append(name)
    return nullable


def _collect_pushdown(
    tree: exp.Expression, model: SemanticModel
) -> dict[str, list[exp.Expression]]:
    """Predicates that mention exactly one entity can move inside its derived table.

    This is a real win on a legacy schema: filtering inside the subquery means the
    normalisation expressions run over fewer rows, and on SQL Server it lets the
    optimiser use an index the outer query could not.

    Only predicates joined by AND at the top level are moved, and only when every
    column in them belongs to one alias. Anything under an OR is left alone.
    """
    out: dict[str, list[exp.Expression]] = {}
    where = tree.args.get("where") if isinstance(tree, exp.Select) else None
    if where is None:
        return out

    aliases = _alias_map(tree, model)
    nullable = _nullable_aliases(tree)

    def conjuncts(node: exp.Expression):
        if isinstance(node, exp.And):
            yield from conjuncts(node.this)
            yield from conjuncts(node.expression)
        else:
            yield node

    for predicate in conjuncts(where.this):
        owners = {
            column.table.lower()
            for column in predicate.find_all(exp.Column)
            if column.table
        }
        if len(owners) != 1:
            continue
        owner = next(iter(owners))
        if owner not in aliases or owner in nullable:
            continue
        if any(predicate.find_all(exp.Select)):
            continue          # a correlated subquery cannot be moved safely
        # Strip the qualifier: inside the derived table the column is unqualified.
        copy = predicate.copy()
        for column in copy.find_all(exp.Column):
            column.set("table", None)
        out.setdefault(owner, []).append(copy)
    return out


def _to_physical(
    predicate: exp.Expression, entity: Entity, profile: CapabilityProfile
) -> exp.Expression | None:
    """Rewrite a logical predicate over the entity's *physical* columns.

    A pushed-down predicate lands inside the derived table, below the projection that
    defines the logical names. Left as written, a logical name there resolves to
    whatever physical column happens to share it: `cfnm = 'AUSTIN'` compared the raw,
    space-padded column rather than the trimmed one the outer query sees, and matched
    nothing — with no error to say so. Each logical column is replaced by its own
    expression; a predicate that cannot be translated is simply not pushed, since the
    outer query applies it anyway.
    """
    translated = predicate.copy()
    for column in list(translated.find_all(exp.Column)):
        logical = entity.column(column.name)
        if logical is None:
            return None
        try:
            physical = sqlglot.parse_one(logical.expr, read=profile.sqlglot_dialect)
        except sqlglot.errors.ParseError:
            return None
        column.replace(physical if isinstance(physical, exp.Column) else exp.Paren(this=physical))
    return translated


def _fold_case(tree: exp.Expression, model: SemanticModel) -> None:
    """Compare literals with case-insensitive columns in upper case on both sides.

    "Austin Cintron" against a column loaded in capitals matches nothing, and the
    value index cannot help: 599 names are data, not a vocabulary. The model marks
    such columns from their profile; here each equality, IN list or LIKE between one
    of them and a string literal is folded, so the question's spelling stops mattering.
    """
    aliases = _alias_map(tree, model)
    only = next(iter(aliases.values())) if len(aliases) == 1 else None

    def insensitive(column: exp.Column) -> bool:
        owner = aliases.get(column.table.lower()) if column.table else only
        entity = model.entity(owner) if owner else None
        logical = entity.column(column.name) if entity else None
        return bool(logical and logical.case_insensitive)

    def upper_literal(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal) and node.is_string:
            return exp.Literal.string(node.this.upper())
        return node

    for node in list(tree.find_all(exp.EQ, exp.NEQ, exp.Like, exp.In)):
        left = node.this
        if not isinstance(left, exp.Column) or not insensitive(left):
            continue
        if isinstance(node, exp.In):
            values = node.expressions
            if not values or not all(isinstance(v, exp.Literal) and v.is_string for v in values):
                continue
            node.set("expressions", [upper_literal(v) for v in values])
        else:
            right = node.expression
            if not (isinstance(right, exp.Literal) and right.is_string):
                continue
            node.set("expression", upper_literal(right))
        left.replace(exp.Upper(this=left.copy()))


def _alias_map(tree: exp.Expression, model: SemanticModel) -> dict[str, str]:
    """alias -> entity name, for every entity reference in the query."""
    out: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        entity = model.entity(table.name)
        if entity is None:
            continue
        alias = (table.alias or table.name).lower()
        out[alias] = entity.name
    return out


# ------------------------------------------------------------------ logical schema text


def render_logical_schema(
    model: SemanticModel,
    entities: list[str] | None = None,
    *,
    max_columns: int = 24,
) -> str:
    """The compact schema serialisation the generator prompt carries.

    One line per column (PLAN.md 4.6): the logical name, its type, a description when
    there is one, and the enum labels when the column is a code. Physical names,
    expressions and table classifications are all omitted — the model has no use for
    them and they cost tokens.
    """
    names = entities or sorted(model.entities)
    lines: list[str] = []
    for name in names:
        entity = model.entity(name)
        if entity is None:
            continue
        header = f"{entity.name}"
        if entity.description:
            header += f" — {entity.description}"
        if entity.row_estimate:
            header += f"  (~{entity.row_estimate:,} rows)"
        lines.append(header)

        shown = 0
        for column in entity.columns.values():
            if column.status == "rejected":
                continue
            if shown >= max_columns:
                lines.append(f"  … {len(entity.columns) - shown} more columns")
                break
            shown += 1
            line = f"  {entity.name}.{column.name} {column.data_type}"
            if column.description:
                line += f" — {column.description}"
            if column.enum:
                labels = ", ".join(
                    f"{k}={v}" for k, v in list(column.enum.items())[:8]
                )
                line += f"  values: {labels}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


def render_relationships(model: SemanticModel, entities: list[str] | None = None) -> str:
    """The join conditions, in logical terms, for the generator prompt.

    The model is given these rather than being asked to invent them: join conditions
    are the single thing a language model gets wrong most expensively on a schema with
    no declared foreign keys.
    """
    wanted = {e.lower() for e in entities} if entities else None
    lines: list[str] = []
    for relationship in model.relationships:
        if relationship.status == "rejected":
            continue
        if wanted is not None and not (
            relationship.from_entity.lower() in wanted
            and relationship.to_entity.lower() in wanted
        ):
            continue
        if not relationship.logical_on:
            continue
        line = f"  {relationship.from_entity} -> {relationship.to_entity}: ON {relationship.logical_on}"
        if relationship.condition:
            line += f"  (only when {relationship.condition})"
        lines.append(line)
    return "\n".join(lines)
