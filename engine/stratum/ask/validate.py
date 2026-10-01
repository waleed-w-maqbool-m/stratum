"""Validation before execution (PLAN.md 4.4 step 8).

Six checks, in the order they are cheapest to run:

1. **Single SELECT** — the read-only guard, re-asserted on the compiled SQL rather
   than trusting that the logical SQL was safe.
2. **Objects exist** — every entity and column referenced is in the model. This is
   where LinkedIn's "fixer" catches a hallucinated column, and it costs nothing.
3. **Default filters present** — the compiler applies these, so their absence means
   the compiler was bypassed. Treated as an internal error, not a style note.
4. **Profile linter** — CLAUDE.md non-negotiable #5.
5. **Fan-out** — the expensive-to-get-wrong one. Aggregating over a one-to-many join
   double counts, silently. Compared with a real probe.
6. **Cost guard** — is this query going to read the whole audit log?

A check that cannot be run returns `unknown` rather than passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from ..connectors.costguard import CostGuard, CostVerdict
from ..connectors.executor import ReadOnlyExecutor, ReadOnlyViolation, assert_read_only
from ..semantic.model import SemanticModel
from . import dialect as dialect_module
from .compiler import CompiledQuery, select_from, set_select_from


@dataclass
class Check:
    name: str
    passed: bool | None          # None = could not be determined
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.passed is False


@dataclass
class ValidationResult:
    checks: list[Check] = field(default_factory=list)
    cost: CostVerdict | None = None

    @property
    def ok(self) -> bool:
        return not any(c.failed for c in self.checks)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def warnings(self) -> list[str]:
        return [c.message for c in self.checks if c.passed is None and c.message]

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "passed": c.passed, "message": c.message,
                 "detail": c.detail}
                for c in self.checks
            ],
            "cost": self.cost.to_dict() if self.cost else None,
        }


def validate(
    compiled: CompiledQuery,
    model: SemanticModel,
    executor: ReadOnlyExecutor,
    *,
    cost_guard: CostGuard | None = None,
    check_fan_out: bool = True,
) -> ValidationResult:
    result = ValidationResult()
    profile = executor.profile
    sql = compiled.physical_sql

    # --- 1. read-only
    try:
        assert_read_only(sql, profile.sqlglot_dialect)
        result.add(Check("read_only", True))
    except ReadOnlyViolation as exc:
        result.add(Check("read_only", False, str(exc)))
        return result       # nothing else is worth checking

    # --- 2. referenced objects exist
    result.add(_check_objects(compiled, model))

    # --- 3. the compiler's default filters survived
    result.add(_check_default_filters(compiled, model))

    # --- 4. profile linter
    lint = dialect_module.lint(sql, profile)
    result.add(
        Check(
            "profile_linter",
            lint.ok,
            lint.summary() if not lint.ok else f"passes {profile.name}",
            {"violations": [str(v) for v in lint.violations]},
        )
    )

    # --- 5. fan-out
    if check_fan_out:
        result.add(_check_fan_out(compiled, model, executor))

    # --- 6. cost guard
    if cost_guard is not None:
        verdict = cost_guard.check(sql)
        result.cost = verdict
        result.add(
            Check(
                "cost_guard",
                verdict.allowed,
                verdict.reason or "within budget",
                verdict.to_dict(),
            )
        )
    return result


def _check_objects(compiled: CompiledQuery, model: SemanticModel) -> Check:
    """Every entity and logical column in the *logical* SQL must exist.

    Checked against the logical SQL rather than the physical: by the time the compiler
    has run, unknown names have already raised, and a model referencing a column that
    does not exist is the failure mode worth naming precisely for the repair loop.
    """
    try:
        tree = sqlglot.parse_one(compiled.logical_sql)
    except Exception as exc:
        return Check("objects_exist", None, f"logical SQL did not re-parse: {exc}")

    local_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}

    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        if table.name.lower() in local_names:
            continue        # a CTE the query defines, not an entity
        entity = model.entity(table.name)
        if entity is None:
            return Check(
                "objects_exist", False,
                f"unknown entity {table.name!r}",
                {"available": sorted(model.entities)[:30]},
            )
        aliases[(table.alias or table.name).lower()] = entity.name

    # Everything the query defines for itself. The check is about catching a column the
    # *model* does not have, not about re-implementing SQL name resolution — so names a
    # query introduces are simply out of scope for it.
    #
    # Three kinds, all of which previously read as "column does not exist" and sent a
    # correct query to the repair loop:
    #   SELECT SUM(x) AS revenue ... GROUP BY revenue     — a projection alias
    #   WITH sol AS (...) SELECT sol.revenue FROM sol     — a CTE and its columns
    #   FROM (SELECT ... AS revenue) sol                  — a derived table
    defined: set[str] = set()
    derived: set[str] = set()

    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            alias = getattr(projection, "alias_or_name", "")
            if alias:
                defined.add(alias.lower())

    derived |= local_names
    for subquery in tree.find_all(exp.Subquery):
        if subquery.alias:
            derived.add(subquery.alias.lower())

    unknown: list[str] = []
    for column in tree.find_all(exp.Column):
        qualifier = column.table.lower()
        name = column.name
        if not name or name == "*":
            continue
        if qualifier and qualifier in derived:
            continue        # a column of something this query built
        if not qualifier and name.lower() in defined:
            continue        # an alias this query introduced
        candidates = (
            [aliases[qualifier]] if qualifier in aliases else list(aliases.values())
        )
        if not candidates:
            continue
        found = False
        for entity_name in candidates:
            entity = model.entity(entity_name)
            if entity is not None and entity.column(name) is not None:
                found = True
                break
        if not found:
            unknown.append(f"{qualifier + '.' if qualifier else ''}{name}")

    if unknown:
        available = {
            name: sorted(model.entity(name).columns)[:18]
            for name in set(aliases.values())
            if model.entity(name)
        }
        return Check(
            "objects_exist", False,
            "these columns do not exist: " + ", ".join(sorted(set(unknown))[:6]),
            {"unknown": sorted(set(unknown)), "available": available},
        )
    return Check("objects_exist", True)


def _check_default_filters(compiled: CompiledQuery, model: SemanticModel) -> Check:
    """Each entity used must have contributed its default filters.

    The compiler applies these, so a miss means something bypassed the compiler — for
    instance a repair that edited the physical SQL directly. Worth catching loudly,
    because the symptom is a count that is quietly too high.
    """
    # sqlglot re-renders the filter when it writes the derived table, so
    # `COALESCE(DEL_FLG,'N')` comes back as `COALESCE(DEL_FLG, 'N')`. Compare with all
    # whitespace removed rather than chasing the formatter — and with identifier quotes
    # removed too: the compiler quotes `"dtmDltd"` on Postgres, and a check comparing it
    # to the model's unquoted `dtmDltd` reported the filter missing, so the repair loop
    # "fixed" a correct query by adding the filter again on a column it could not see.
    def squash(text: str) -> str:
        return "".join(text.split()).upper().translate(str.maketrans("", "", '"[]`'))

    compiled_squashed = squash(compiled.physical_sql)
    missing: list[str] = []
    for entity_name in compiled.entities_used:
        entity = model.entity(entity_name)
        if entity is None:
            continue
        for filt in entity.active_filters():
            if squash(filt.expr) not in compiled_squashed:
                missing.append(f"{entity_name}: {filt.expr}")
    if missing:
        return Check(
            "default_filters", False,
            "the compiled SQL is missing filters the model requires: "
            + "; ".join(missing[:3]),
            {"missing": missing},
        )
    return Check("default_filters", True)


def _check_fan_out(
    compiled: CompiledQuery, model: SemanticModel, executor: ReadOnlyExecutor
) -> Check:
    """Does a one-to-many join multiply the rows an aggregate reads?

    Summing an order total across a join to order lines returns each order's total once
    per line. Nothing errors and the number looks plausible, which is what makes it
    worth a real query to rule out.

    The probe compares the row count the aggregate sees against the distinct count of
    the aggregated entity's key. Only run when the query actually aggregates.
    """
    try:
        tree = sqlglot.parse_one(compiled.logical_sql)
    except Exception:
        return Check("fan_out", None, "could not parse the logical SQL")

    aggregates = [
        node for node in tree.find_all(exp.AggFunc)
        if not isinstance(node, exp.Count)
    ]
    if not aggregates:
        return Check("fan_out", True, "no aggregate over a joined column")

    joins = list(tree.find_all(exp.Join))
    if not joins:
        return Check("fan_out", True, "no join")

    # Which entity owns each aggregated column?
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        entity = model.entity(table.name)
        if entity is not None:
            aliases[(table.alias or table.name).lower()] = entity.name

    unprobed: list[str] = []
    at_risk: set[str] = set()
    for aggregate in aggregates:
        for column in aggregate.find_all(exp.Column):
            owner = aliases.get(column.table.lower())
            if owner:
                at_risk.add(owner)

    if not at_risk:
        return Check("fan_out", None, "could not attribute the aggregate to an entity")

    # Probe: run the query's FROM/JOIN/WHERE and compare counts.
    from .compiler import compile_logical

    for entity_name in at_risk:
        entity = model.entity(entity_name)
        if entity is None or not entity.key:
            continue
        # The whole key, not just its first column: an order line is identified by
        # (STORE_CD, ORD_NO, LN_SEQ), and counting distinct STORE_CD would say the
        # database holds four order lines.
        key_columns: list[str] = []
        for physical in entity.key:
            match = next(
                (
                    c.name for c in entity.columns.values()
                    if c.physical_column.lower() == physical.lower()
                ),
                None,
            )
            if match is not None:
                key_columns.append(match)
        if len(key_columns) != len(entity.key) or not key_columns:
            unprobed.append(entity_name)
            continue

        alias = next(
            (a for a, name in aliases.items() if name == entity_name), entity_name
        )
        probes = _rebuild_probe(tree, alias, key_columns)
        if probes is None:
            unprobed.append(entity_name)
            continue
        try:
            total_sql, distinct_sql = probes
            total_compiled = compile_logical(total_sql, model, executor.profile)
            distinct_compiled = compile_logical(distinct_sql, model, executor.profile)
            total_rows = executor.run(total_compiled.physical_sql, row_cap=2)
            distinct_rows = executor.run(distinct_compiled.physical_sql, row_cap=2)
        except Exception:
            unprobed.append(entity_name)
            continue
        if not total_rows.rows or not distinct_rows.rows:
            unprobed.append(entity_name)
            continue
        total = total_rows.rows[0][0] or 0
        distinct = distinct_rows.rows[0][0] or 0
        if distinct and total > distinct:
            return Check(
                "fan_out", False,
                (
                    f"the join multiplies {entity_name}: the aggregate reads {total:,} "
                    f"rows for {distinct:,} distinct {entity_name} records, so any SUM "
                    "or AVG over it is counted more than once"
                ),
                {"rows": total, "distinct": distinct, "entity": entity_name},
            )
    if unprobed:
        # The probe could not be built or run. Saying "no fan-out" here would be a
        # claim the check never established, and fan-out is silently wrong when missed.
        return Check(
            "fan_out", None,
            "could not confirm whether the join multiplies "
            + ", ".join(sorted(set(unprobed))),
            {"unprobed": sorted(set(unprobed))},
        )
    return Check("fan_out", True, "aggregates are not multiplied by the joins")


def _rebuild_probe(
    tree: exp.Expression, alias: str, key_columns: list[str]
) -> tuple[str, str] | None:
    """Two queries over the original FROM/JOIN/WHERE: how many rows, and how many
    distinct key tuples.

    Returned as a pair rather than one query because `COUNT(DISTINCT a, b, c)` is not
    portable — SQLite and SQL Server both reject it — and a subquery is.
    """
    if not isinstance(tree, exp.Select):
        return None
    from_clause = select_from(tree)
    if from_clause is None:
        return None

    def scaffold(expressions: list[exp.Expression], distinct: bool = False) -> exp.Select:
        select = exp.Select(expressions=expressions)
        if distinct:
            select.set("distinct", exp.Distinct())
        set_select_from(select, from_clause.copy())
        if tree.args.get("joins"):
            select.set("joins", [j.copy() for j in tree.args["joins"]])
        if tree.args.get("where"):
            select.set("where", tree.args["where"].copy())
        return select

    total = scaffold([exp.Count(this=exp.Star())])
    # `exp.column("a.b")` would build one identifier named "a.b"; the qualifier has to
    # be passed separately or the probe selects a column that does not exist.
    keys = scaffold(
        [exp.column(name, table=alias) for name in key_columns], distinct=True
    )
    distinct_count = exp.Select(expressions=[exp.Count(this=exp.Star())]).from_(
        exp.Subquery(this=keys, alias=exp.TableAlias(this=exp.to_identifier("_keys")))
    )
    return total.sql(), distinct_count.sql()
