"""Capability linting and downgrade rewriting.

CLAUDE.md non-negotiable #5: generated SQL for a connection must pass that connection's
profile linter. `tsql2008` forbids OFFSET/FETCH, IIF, CONCAT, TRY_CAST, FORMAT,
LAG/LEAD, JSON functions and STRING_AGG, and the container being SQL Server 2022 does
not change that — the shop runs at compatibility level 100.

Two halves:

* `lint` reports every construct the profile forbids, with the offending SQL.
* `downgrade` rewrites what can be rewritten — `LIMIT` to `TOP`, a CTE to a derived
  table, `CONCAT` to `+` with `ISNULL`, `TRY_CAST` to a `CASE` — and reports what
  cannot, so the caller can escalate to the Python fallback (PLAN.md 4.5, R3).

The rewriter works on the sqlglot tree, never on the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..connectors.profiles import CapabilityProfile


@dataclass
class Violation:
    feature: str
    message: str
    sql: str
    rewritable: bool

    def __str__(self) -> str:
        return f"{self.feature}: {self.message}"


@dataclass
class LintResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if not v.rewritable]

    def summary(self) -> str:
        if self.ok:
            return "passes the profile linter"
        return "; ".join(str(v) for v in self.violations[:5])


# What each capability flag forbids, keyed by the sqlglot expression *class* rather
# than by the spelling in the source.
#
# This is not a style preference. sqlglot canonicalises as it parses: `IIF` arrives as
# `If`, `STRING_AGG` as `GroupConcat`, `JSON_VALUE` as `JSONExtractScalar` and T-SQL's
# `FORMAT` as `TimeToStr`. Matching the name the user typed silently missed all of
# those, and a linter that misses what it is for is worse than none.
#
# (flag, expression class, human message, rewritable, dialects it applies to)
#
# The dialect restriction matters because a canonical class is shared across dialects
# that do not share the restriction. `TimeToStr` is T-SQL's `FORMAT` *and* SQLite's
# `STRFTIME`: forbidding the class outright rejected SQLite's own date function as a
# SQL Server 2012 feature.
def _rules() -> list[tuple[str, type, str, bool, frozenset[str] | None]]:
    out: list[tuple[str, type, str, bool, frozenset[str] | None]] = []

    def add(
        flag: str,
        class_name: str,
        message: str,
        rewritable: bool,
        dialects: frozenset[str] | None = None,
    ) -> None:
        node_type = getattr(exp, class_name, None)
        if node_type is not None:
            out.append((flag, node_type, message, rewritable, dialects))

    add("iif", "If", "IIF was added in SQL Server 2012; use CASE", True)
    add("concat_fn", "Concat", "CONCAT was added in 2012; use + with ISNULL", True)
    add("try_cast", "TryCast", "TRY_CAST and TRY_CONVERT were added in 2012", True)
    add("string_agg", "GroupConcat", "STRING_AGG was added in 2017", False)
    add("json_fns", "JSONExtract", "JSON support was added in 2016", False)
    add("json_fns", "JSONExtractScalar", "JSON support was added in 2016", False)
    add("json_fns", "ParseJSON", "JSON support was added in 2016", False)
    add("json_fns", "JSONFormat", "JSON support was added in 2016", False)
    add("regex", "RegexpLike", "this dialect has no regular expression support", False)
    add("regex", "RegexpReplace", "this dialect has no regular expression support", False)
    add("regex", "RegexpExtract", "this dialect has no regular expression support", False)
    add("lag_lead", "Lag", "LAG was added in SQL Server 2012", False)
    add("lag_lead", "Lead", "LEAD was added in SQL Server 2012", False)
    # T-SQL's FORMAT() parses as TimeToStr, and so does SQLite's STRFTIME. Restrict the
    # rule to T-SQL, where the class actually renders as the forbidden function.
    add(
        "concat_fn", "TimeToStr", "FORMAT was added in SQL Server 2012", False,
        frozenset({"tsql"}),
    )
    return out


FUNCTION_RULES = _rules()


def _is_standalone_iif(node: exp.Expression) -> bool:
    """True for a real `IIF(...)`, false for a `WHEN` branch of a `CASE`.

    sqlglot uses `exp.If` for both, so `CASE WHEN a > 1 THEN 'y' END` contains an
    `If` node with no `IIF` anywhere in sight. Flagging those would reject the very
    construct the rewrite produces, and rewriting them would nest a CASE inside each of
    its own branches.
    """
    return not isinstance(node.parent, exp.Case)

# Functions sqlglot does not model, matched by name as a last resort.
_ANONYMOUS_RULES: dict[str, tuple[str, str, bool]] = {
    "openjson": ("json_fns", "JSON support was added in 2016", False),
    "try_convert": ("try_cast", "TRY_CONVERT was added in 2012", True),
    "json_modify": ("json_fns", "JSON support was added in 2016", False),
}


def lint(sql: str, profile: CapabilityProfile) -> LintResult:
    """Every construct in `sql` that `profile` forbids."""
    try:
        tree = sqlglot.parse_one(sql, read=profile.sqlglot_dialect)
    except Exception as exc:
        return LintResult(
            ok=False,
            violations=[Violation("parse", f"could not parse: {exc}", sql[:200], False)],
        )
    if tree is None:
        return LintResult(ok=False, violations=[Violation("parse", "empty", sql, False)])

    violations: list[Violation] = []

    def add(feature: str, message: str, node: exp.Expression, rewritable: bool) -> None:
        violations.append(Violation(feature, message, node.sql()[:160], rewritable))

    # --- CTEs
    if not profile.cte:
        for node in tree.find_all(exp.With):
            add("cte", "common table expressions are not available", node, True)

    # --- window functions
    for node in tree.find_all(exp.Window):
        if not profile.window_basic:
            add("window_basic", "window functions are not available", node, False)
            continue
        if node.args.get("spec") is not None and not profile.window_frame:
            add(
                "window_frame",
                "ROWS/RANGE window frames were added in SQL Server 2012",
                node,
                False,
            )
    # --- paging
    for node in tree.find_all(exp.Offset):
        if not profile.offset_fetch:
            add("offset_fetch", "OFFSET/FETCH was added in SQL Server 2012", node, False)
    for node in tree.find_all(exp.Limit):
        if not profile.limit and not profile.top:
            add("limit", "this profile supports neither LIMIT nor TOP", node, False)

    # --- set operations
    if not profile.except_intersect:
        for node_type in (exp.Except, exp.Intersect):
            for node in tree.find_all(node_type):
                add(
                    "except_intersect",
                    f"{node_type.__name__.upper()} is not available",
                    node, False,
                )
    if not profile.full_outer_join:
        for node in tree.find_all(exp.Join):
            if (node.args.get("side") or "").upper() == "FULL":
                add("full_outer_join", "FULL OUTER JOIN is not available", node, False)

    # --- functions, matched on the parsed class
    for flag, node_type, message, rewritable, dialects in FUNCTION_RULES:
        if profile.allows(flag):
            continue
        if dialects is not None and profile.sqlglot_dialect not in dialects:
            continue
        for node in tree.find_all(node_type):
            if node_type is exp.If and not _is_standalone_iif(node):
                continue
            add(flag, message, node, rewritable)

    # --- functions sqlglot left anonymous
    for node in tree.find_all(exp.Anonymous):
        name = str(node.this or "").lower()
        rule = _ANONYMOUS_RULES.get(name)
        if rule is not None and not profile.allows(rule[0]):
            add(rule[0], rule[1], node, rule[2])

    # --- ILIKE
    if not profile.ilike:
        for node in tree.find_all(exp.ILike):
            add("ilike", "ILIKE is not available; use UPPER() on both sides", node, True)

    return LintResult(ok=not violations, violations=violations)


# --------------------------------------------------------------------------- rewriting


@dataclass
class DowngradeResult:
    sql: str
    applied: list[str] = field(default_factory=list)
    unresolved: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved


def downgrade(sql: str, profile: CapabilityProfile) -> DowngradeResult:
    """Rewrite `sql` into what `profile` allows, as far as that is possible."""
    try:
        tree = sqlglot.parse_one(sql, read=profile.sqlglot_dialect)
    except Exception as exc:
        return DowngradeResult(
            sql=sql,
            unresolved=[Violation("parse", str(exc), sql[:200], False)],
        )

    applied: list[str] = []

    tree, changed = _rewrite_date_parts(tree, profile)
    if changed:
        applied.append("EXTRACT -> this dialect's date-part idiom")

    if not profile.iif:
        tree, changed = _rewrite_iif(tree)
        if changed:
            applied.append("IIF -> CASE")
    if not profile.concat_fn:
        tree, changed = _rewrite_concat(tree, profile)
        if changed:
            applied.append("CONCAT -> string concatenation with null handling")
    if not profile.try_cast:
        tree, changed = _rewrite_try_cast(tree, profile)
        if changed:
            applied.append("TRY_CAST -> CASE guarded CAST")
    if not profile.ilike:
        tree, changed = _rewrite_ilike(tree)
        if changed:
            applied.append("ILIKE -> UPPER(...) LIKE UPPER(...)")
    if not profile.cte:
        tree, changed = _inline_ctes(tree)
        if changed:
            applied.append("CTE -> derived table")

    rewritten = tree.sql(dialect=profile.sqlglot_dialect)
    # LIMIT/TOP needs no rewriting: sqlglot renders the same `limit` node as whichever
    # the write dialect uses.
    result = lint(rewritten, profile)
    return DowngradeResult(sql=rewritten, applied=applied, unresolved=result.violations)


# SQLite's STRFTIME format codes, by the date part being asked for.
_STRFTIME_CODES = {
    "YEAR": "%Y", "MONTH": "%m", "DAY": "%d",
    "HOUR": "%H", "MINUTE": "%M", "SECOND": "%S",
    "WEEK": "%W", "DAYOFYEAR": "%j",
}


# Dialects that fold an unquoted identifier to lower case, so that any name spelled
# otherwise in the catalog only resolves when quoted. SQLite and SQL Server compare
# identifiers case-insensitively and never need it.
_FOLDS_TO_LOWER = {"postgres"}
_PLAIN_LOWER = re.compile(r"^[a-z_][a-z0-9_$]*$")


def needs_quoting(name: str, dialect: str) -> bool:
    """Would this catalog name fail to resolve if written bare?

    `tblFilm` on Postgres: unquoted it becomes `tblfilm`, and that relation does not
    exist. The check is on the catalog's own spelling, so it never quotes a name that
    works bare — only ones that are guaranteed to fail without it.
    """
    if dialect not in _FOLDS_TO_LOWER:
        return False
    return not _PLAIN_LOWER.match(name)


def quote_catalog_identifiers(tree: exp.Expression, names: set[str]) -> exp.Expression:
    """Quote every bare identifier that is one of `names`, exactly as spelled.

    Applied to SQL the engine itself writes from catalog names: the compiled physical
    query, and excavation's own probes. Deliberately *not* applied to raw model output
    such as the naive baseline's, where it would quietly repair a mistake the baseline
    is supposed to be measured on.
    """
    if not names:
        return tree
    for ident in tree.find_all(exp.Identifier):
        if not ident.quoted and ident.name in names:
            ident.set("quoted", True)
    return tree


def quote_catalog_sql(sql: str, names: set[str], dialect: str) -> str:
    """`quote_catalog_identifiers` for SQL that exists only as text."""
    if not names or dialect not in _FOLDS_TO_LOWER:
        return sql
    if not any(name in sql for name in names):
        return sql
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return sql
    return quote_catalog_identifiers(tree, names).sql(dialect="postgres")


def normalise_for_dialect(
    tree: exp.Expression, profile: CapabilityProfile
) -> tuple[exp.Expression, bool]:
    """Rewrite standard date idioms *before* the tree is rendered.

    This has to run on the ANSI tree rather than on the emitted SQL. Rendering
    `TO_CHAR(d, 'YYYY-MM')` for SQLite discards the format argument and leaves a bare
    CAST — by the time `downgrade` sees the text, the information needed to rewrite it
    correctly is already gone.
    """
    return _rewrite_date_parts(tree, profile)


def _rewrite_date_parts(
    tree: exp.Expression, profile: CapabilityProfile
) -> tuple[exp.Expression, bool]:
    """`EXTRACT(YEAR FROM x)` in whatever this dialect actually offers.

    sqlglot maps EXTRACT onto T-SQL's DATEPART and leaves Postgres alone, but SQLite has
    no equivalent and sqlglot passes the call through unchanged — so the failure arrives
    at the database rather than at the compiler. SQLite gets STRFTIME, cast back to an
    integer so comparisons against a bare year still work.
    """
    if profile.sqlglot_dialect != "sqlite":
        return tree, False

    changed = False
    for node in list(tree.find_all(exp.Extract)):
        part = (node.this.name if node.this else "").upper()
        code = _STRFTIME_CODES.get(part)
        target = node.expression
        if code is None or target is None:
            continue
        strftime = exp.Anonymous(
            this="STRFTIME",
            expressions=[exp.Literal.string(code), target.copy()],
        )
        node.replace(
            exp.Cast(this=strftime, to=exp.DataType.build("INT"))
        )
        changed = True

    # `TO_CHAR(d, 'YYYY-MM')` is the Postgres idiom for a month bucket. sqlglot cannot
    # render it for SQLite at all — it raises while generating, which surfaces as an
    # unexplained failure rather than a rewritable violation — so translate the format
    # string into STRFTIME's codes directly.
    to_char = getattr(exp, "ToChar", None)
    if to_char is not None:
        for node in list(tree.find_all(to_char)):
            target = node.this
            fmt = node.args.get("format")
            if target is None or fmt is None or not isinstance(fmt, exp.Literal):
                continue
            code = _to_strftime_format(fmt.name)
            if code is None:
                continue
            node.replace(
                exp.Anonymous(
                    this="STRFTIME",
                    expressions=[exp.Literal.string(code), target.copy()],
                )
            )
            changed = True
    return tree, changed


def _to_strftime_format(pattern: str) -> str | None:
    """Translate an Oracle/Postgres date pattern into STRFTIME codes.

    Only the pieces that appear in a date bucket. Anything unrecognised returns None so
    the caller leaves the expression alone rather than emitting a plausible-looking
    format that quietly produces the wrong string.
    """
    replacements = [
        ("YYYY", "%Y"), ("YY", "%y"), ("MM", "%m"), ("DD", "%d"),
        ("HH24", "%H"), ("HH", "%H"), ("MI", "%M"), ("SS", "%S"),
    ]
    out = pattern
    for source, target in replacements:
        out = out.replace(source, target)
    # Every remaining character must be a separator, or the pattern used a directive we
    # do not understand and guessing would produce a plausible-looking wrong string.
    # The whole `%X` pair has to come out, not just the `%` — `%Y-%m` is all letters.
    leftovers = re.sub(r"%.", "", out)
    if any(c.isalpha() for c in leftovers):
        return None
    return out


def _rewrite_iif(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    changed = False
    for node in list(tree.find_all(exp.If)):
        if not _is_standalone_iif(node):
            continue        # a CASE branch, not an IIF call
        condition = node.this
        true_value = node.args.get("true")
        false_value = node.args.get("false")
        if condition is None:
            continue
        case = exp.Case(
            ifs=[exp.If(this=condition, true=true_value)],
            default=false_value,
        )
        node.replace(case)
        changed = True
    return tree, changed


def _rewrite_concat(
    tree: exp.Expression, profile: CapabilityProfile
) -> tuple[exp.Expression, bool]:
    """`CONCAT(a, b, c)` becomes `ISNULL(a,'') + ISNULL(b,'') + ISNULL(c,'')`.

    The null handling is the point: CONCAT treats NULL as empty, whereas `+` propagates
    it, so a naive rewrite silently turns rows into NULL.
    """
    changed = False
    operator = "||" if profile.sqlglot_dialect in ("sqlite", "postgres") else "+"
    for node in list(tree.find_all(exp.Concat)):
        parts = node.expressions or []
        if not parts:
            continue
        guarded = [
            exp.Coalesce(this=part, expressions=[exp.Literal.string("")])
            for part in parts
        ]
        combined: exp.Expression = guarded[0]
        for part in guarded[1:]:
            combined = (
                exp.DPipe(this=combined, expression=part)
                if operator == "||"
                else exp.Add(this=combined, expression=part)
            )
        node.replace(combined)
        changed = True
    return tree, changed


def _rewrite_try_cast(
    tree: exp.Expression, profile: CapabilityProfile
) -> tuple[exp.Expression, bool]:
    """`TRY_CAST(x AS INT)` becomes a CASE that only casts what will convert.

    On `tsql2008` there is no TRY_CAST and ISNUMERIC is unreliable (it accepts '1e5'
    and '$1'), so the guard is a LIKE pattern that admits only digit strings. This is
    the same reasoning behind comparing zero-padded keys as strings rather than casting
    the dirty side (PLAN.md 4.3).
    """
    changed = False
    for node in list(tree.find_all(exp.TryCast)):
        value = node.this
        to = node.to
        type_name = (to.this.name if to and to.this else "").upper()
        if type_name in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
            guard = exp.Not(
                this=exp.Like(
                    this=value,
                    expression=exp.Literal.string("%[^0-9]%"),
                )
                if profile.sqlglot_dialect == "tsql"
                else exp.Like(this=value, expression=exp.Literal.string("%[^0-9]%"))
            )
        else:
            guard = exp.Is(this=value, expression=exp.Null()).not_()
        case = exp.Case(
            ifs=[exp.If(this=guard, true=exp.Cast(this=value, to=to))],
            default=exp.Null(),
        )
        node.replace(case)
        changed = True
    return tree, changed


def _rewrite_ilike(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    changed = False
    for node in list(tree.find_all(exp.ILike)):
        node.replace(
            exp.Like(
                this=exp.Upper(this=node.this),
                expression=exp.Upper(this=node.expression),
            )
        )
        changed = True
    return tree, changed


def _inline_ctes(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    """Replace `WITH a AS (...) SELECT ... FROM a` with a derived table.

    Only handles non-recursive CTEs referenced in the FROM or JOIN clauses, which is
    what the generator produces. A recursive CTE cannot be inlined and is left for the
    linter to refuse.
    """
    with_node = tree.args.get("with_") or tree.args.get("with") or tree.find(exp.With)
    if with_node is None:
        return tree, False
    if with_node.args.get("recursive"):
        return tree, False

    definitions: dict[str, exp.Expression] = {}
    for cte in with_node.expressions:
        alias = cte.alias_or_name
        if alias:
            definitions[alias.lower()] = cte.this

    if not definitions:
        return tree, False

    changed = False
    for table in list(tree.find_all(exp.Table)):
        name = table.name.lower()
        if name not in definitions:
            continue
        body = definitions[name].copy()
        alias = table.alias or table.name
        table.replace(exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        changed = True

    if changed:
        for key in ("with_", "with"):
            if key in type(tree).arg_types:
                tree.set(key, None)
    return tree, changed


def transpile(sql: str, source_dialect: str, profile: CapabilityProfile) -> str:
    """Parse in one dialect and emit in the profile's, without downgrading."""
    return sqlglot.transpile(
        sql, read=source_dialect or None, write=profile.sqlglot_dialect
    )[0]
