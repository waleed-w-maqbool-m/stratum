"""Compile detector findings into a semantic model (PLAN.md 4.2 step 5).

The detectors each know one thing. This is where those become a coherent logical
schema: a god table becomes three entities, a soft-delete finding becomes a default
filter on one of them, a type mismatch and a quality issue on the same column compose
into a single normalising expression, and an embedded XML blob becomes five ordinary
logical columns.

Composition order within a column matters and is fixed here:

    physical column -> type normalisation -> quality canonicalisation -> drift resolution

Getting that order wrong produces expressions that are individually reasonable and
jointly wrong — trimming after casting to integer, for instance.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..origin import Origin, derive_origin, strongest
from ..semantic.model import (
    Column,
    DefaultFilter,
    DriftNote,
    EmbeddedFormat,
    Entity,
    Gap,
    GodTable,
    PerfHint,
    Relationship,
    SemanticModel,
)
from .explain_relationship import describe
from .findings import Finding, Kind, Status
from .introspect import Schema, Table
from .naming import expand
from .pipeline import ExcavationResult
from .profile import ProfileSet


def _edge_key(from_table: str, from_columns: list[str], to_table: str,
              to_columns: list[str]) -> frozenset:
    """An edge with its direction removed, so A->B and B->A compare equal."""
    return frozenset({
        (from_table.lower(), tuple(sorted(c.lower() for c in from_columns))),
        (to_table.lower(), tuple(sorted(c.lower() for c in to_columns))),
    })


def _logical_entity_name(table: Table, taken: set[str]) -> str:
    base = expand(table.name).logical or table.name.lower()
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


# Words that carry no meaning on their own once the entity prefix is stripped.
_BARE_ROLE_WORDS = {"id", "number", "code", "key", "reference", "sequence"}


def _logical_column_name(
    column: str, entity_context: str, taken: set[str], *, is_own_key: bool = False
) -> str:
    """Name a logical column, shortest unambiguous form first.

    Dropping the entity prefix reads better on a table's own columns — `customer.id`,
    not `customer.customer_id` — but it is wrong for a column naming a *different*
    entity. `CUST_CNTCT.CUST_ID` is a reference to the customer; shortened to `id` it
    collides with the contact's own key and loses the only indication of what it
    points at. So the prefix is only dropped for the table's own key, and the full
    expansion is used whenever the short form would be bare or already taken.
    """
    short = expand(column, context=entity_context).logical
    full = expand(column).logical or column.lower()

    candidates = []
    if is_own_key and short:
        candidates.append(short)
    if short and short not in _BARE_ROLE_WORDS:
        candidates.append(short)
    candidates.append(full)
    candidates.append(column.lower())

    for candidate in candidates:
        if candidate and candidate not in taken:
            return candidate

    base = candidates[0] or column.lower()
    name, suffix = base, 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def _case_untrustworthy(profile, quality_findings: list[Finding], enum_findings) -> bool:
    """Should literals be compared with this column case-insensitively?

    Yes when the data says its case carries no meaning: the quality detector saw the
    same value in several cases, or every value is in one case — an all-capitals load
    where "Austin Cintron" is stored as AUSTIN. Not for coded columns: 'X' and 'x' may
    be different codes, and an enum is matched through its labels anyway.
    """
    if profile is None or profile.numeric or profile.temporal or enum_findings:
        return False
    if any((f.value or {}).get("issue") == "case_variants" for f in quality_findings):
        return True
    words = [str(v) for v, _ in profile.top_values if v and any(c.isalpha() for c in str(v))]
    if len(words) < 5:
        return False
    if all(len(w.strip()) <= 3 for w in words):
        return False                     # short codes, where case may be the meaning
    single_case = sum(1 for w in words if w == w.upper() or w == w.lower())
    return single_case / len(words) >= 0.95


def _compose_over_types(resolution: str, columns: list[str], table, of_kind) -> str:
    """Replace each bare column in a drift resolution with its normalised form."""
    import re

    for column in columns:
        if not column:
            continue
        normalised = next(
            (
                f.value["expression"]
                for f in of_kind(f"{table.qualified}.{column}", Kind.TYPE_MISMATCH)
                if f.value.get("expression")
            ),
            None,
        )
        if normalised and normalised != column:
            resolution = re.sub(rf"(?<![\w.]){re.escape(column)}(?!\w)", normalised, resolution)
    return resolution


def _quoted_identifiers(schema: Schema) -> list[str]:
    """Every table and column name this dialect cannot resolve unquoted."""
    from ..ask.dialect import needs_quoting

    names: set[str] = set()
    for table in schema.tables.values():
        for name in (table.name, *(c.name for c in table.columns)):
            if needs_quoting(name, schema.dialect):
                names.add(name)
    return sorted(names)


def compile_model(
    result: ExcavationResult,
    *,
    database: str = "",
    profile_name: str = "",
    version: int = 1,
) -> SemanticModel:
    schema: Schema = result.schema
    profiles: ProfileSet = result.profiles
    findings = result.findings

    model = SemanticModel(
        version=version,
        database=database or schema.database,
        dialect=schema.dialect,
        profile=profile_name or schema.dialect,
        table_classes=dict(result.table_classes),
        quoted_identifiers=_quoted_identifiers(schema),
    )

    # ------------------------------------------------------------- index findings
    by_target: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_target[finding.target.lower()].append(finding)

    def of_kind(target: str, kind: Kind) -> list[Finding]:
        return [
            f for f in by_target.get(target.lower(), [])
            if f.kind is kind and f.status is not Status.REJECTED
        ]

    god_tables = {
        f.value["table"].lower(): f
        for f in findings.of(Kind.GOD_TABLE) if f.status is not Status.REJECTED
    }

    # What enrichment worked out that name expansion could not. Keyed by target, so a
    # table's entry names the entity and a `table.column` entry names the column.
    enriched_names: dict[str, dict[str, Any]] = {}
    for finding in findings.of(Kind.NAMING):
        if finding.status is Status.REJECTED:
            continue
        value = finding.value or {}
        if value.get("logical_name") or value.get("description"):
            enriched_names[finding.target.lower()] = value
    keys_by_table = {
        f.target.lower(): f
        for f in findings.of(Kind.KEY) if f.status is not Status.REJECTED
    }

    # ---------------------------------------------------------------- entities
    taken_names: set[str] = set()
    for table in schema.base_tables:
        if result.table_classes.get(table.qualified, "core") != "core":
            continue
        profile = profiles.get(table.qualified)
        if profile is None:
            continue

        god = god_tables.get(table.qualified.lower())
        if god is not None:
            # One physical table, several logical entities.
            for value, spec in god.value["entities"].items():
                suggested = spec.get("suggested_entity") or f"{table.name.lower()}_{value}"
                name = _logical_entity_name_from(suggested, taken_names)
                taken_names.add(name)
                entity = _build_entity(
                    name=name, table=table, schema=schema, profiles=profiles,
                    of_kind=of_kind, keys_by_table=keys_by_table,
                    discriminator=spec["predicate"],
                    row_estimate=spec.get("rows", 0),
                    exclude_columns=spec.get("never_used"),
                    enriched_names=enriched_names,
                )
                entity.confidence = god.confidence
                model.entities[name] = entity
            continue

        proposed = enriched_names.get(table.qualified.lower(), {})
        expanded = expand(table.name)
        suggested = proposed.get("logical_name") or ""

        # The model's name *replaces* the expansion only when the expansion could not
        # read the name at all. Otherwise it is added as a synonym.
        #
        # Both directions of error are real. `tblPrd` expands confidently to "product"
        # and holds songs, so the expansion alone makes the table unfindable by anyone
        # asking about tracks. But `PLST` expands correctly to "playlist" and the model
        # called it a "category" — renaming on that would have been a clear regression.
        # Keeping the derived name and indexing the suggestion as a synonym gets the
        # retrieval benefit without ever adopting a wrong name.
        if suggested and expanded.is_opaque:
            name = _logical_entity_name_from(suggested, taken_names)
        else:
            name = _logical_entity_name(table, taken_names)
        taken_names.add(name)
        entity = _build_entity(
            name=name, table=table, schema=schema, profiles=profiles,
            of_kind=of_kind, keys_by_table=keys_by_table,
            discriminator="", row_estimate=table.row_estimate, exclude_columns=None,
            enriched_names=enriched_names,
        )
        entity.description = proposed.get("description") or entity.description
        synonyms = list(proposed.get("synonyms") or [])
        if suggested and suggested != name:
            synonyms.append(suggested)
        # Deduplicate while keeping order, so the strongest suggestion stays first.
        entity.synonyms = list(dict.fromkeys(s for s in synonyms if s and s != name))
        model.entities[name] = entity

    # ----------------------------------------------------------- relationships
    def logical_condition(
        from_entity, to_entity, from_columns, to_columns
    ) -> str:
        """Express a join in logical column names.

        The cast that makes the join work lives in the entity column's expression
        (`CAST(CUST_NO AS INTEGER) AS customer_number`), so once both sides are
        projected through their derived tables the condition is a plain equality. That
        is what lets the generator write joins without knowing the storage types
        disagree.
        """
        parts = []
        for left, right in zip(from_columns, to_columns, strict=False):
            left_logical = _logical_for_physical(from_entity, left)
            right_logical = _logical_for_physical(to_entity, right)
            if not left_logical or not right_logical:
                return ""
            parts.append(
                f"{from_entity.name}.{left_logical} = {to_entity.name}.{right_logical}"
            )
        return " AND ".join(parts)

    for finding in findings.of(Kind.JOIN):
        if finding.status is Status.REJECTED:
            continue
        value = finding.value
        from_entity = model.entity_for_table(value["from_table"])
        to_entity = model.entity_for_table(value["to_table"])
        if from_entity is None or to_entity is None:
            continue
        origin = derive_origin(finding.detector, finding.evidence)
        edge = _edge_key(value["from_table"], value["from_columns"],
                         value["to_table"], value["to_columns"])
        twin = next(
            (r for r in model.relationships
             if not r.condition and _edge_key(r.from_table, r.from_columns,
                                              r.to_table, r.to_columns) == edge),
            None,
        )
        if twin is not None:
            # The same edge written down twice, once in either direction. Keep one, under
            # the strongest origin, and remember who else found it.
            winner = strongest([twin.origin, origin])
            others = {twin.origin, origin, *twin.corroborated_by} - {winner}
            twin.corroborated_by = sorted(others)
            twin.origin = winner
            continue
        name = f"{from_entity.name}_{to_entity.name}"
        if any(r.name == name for r in model.relationships):
            name = f"{name}_{len(model.relationships)}"
        evidence = finding.evidence
        model.relationships.append(
            Relationship(
                name=name,
                from_entity=from_entity.name,
                to_entity=to_entity.name,
                from_table=value["from_table"],
                to_table=value["to_table"],
                from_columns=value["from_columns"],
                to_columns=value["to_columns"],
                on=value.get("expression", ""),
                logical_on=logical_condition(
                    from_entity, to_entity, value["from_columns"], value["to_columns"]
                ),
                cardinality=evidence.get("cardinality", "many_to_one"),
                optional=bool(evidence.get("optional", False)),
                origin=origin,
                corroborated_by=list(evidence.get("corroborated_by") or []),
                status=finding.status.value,
                confidence=finding.confidence,
                provenance={
                    "detector": finding.detector,
                    "reason": describe(
                        origin, {**evidence, "needs_cast": value.get("needs_cast")},
                        value["from_table"], value["from_columns"],
                        value["to_table"], value["to_columns"],
                    ),
                    "evidence": finding.evidence,
                },
            )
        )

    # Conditional relationships from overloaded columns: one edge per meaning.
    for finding in findings.of(Kind.OVERLOADED):
        value = finding.value
        if "discriminator" not in value:
            continue
        child = model.entity_for_table(value["table"])
        if child is None:
            continue
        for discriminator_value, spec in value.get("meanings", {}).items():
            target_table = spec.get("target_table")
            if not target_table:
                # The column is overloaded but the target of this meaning could not be
                # identified. Emitting a half-built edge would be worse than none: the
                # join planner would route through it and produce nonsense.
                continue
            target = model.entity_for_table(target_table)
            model.relationships.append(
                Relationship(
                    name=f"{child.name}_ref_{discriminator_value.lower()}",
                    from_entity=child.name,
                    to_entity=target.name if target else "",
                    from_table=value["table"],
                    to_table=target_table,
                    from_columns=[value["column"]],
                    to_columns=[spec.get("target_column", "")],
                    logical_on=(
                        logical_condition(
                            child, target, [value["column"]],
                            [spec.get("target_column", "")],
                        )
                        if target else ""
                    ),
                    condition=spec["predicate"],
                    origin=Origin.CONDITIONAL.value,
                    status="hypothesis",
                    confidence=finding.confidence * 0.85,
                    provenance={
                        "detector": "overloaded",
                        "reason": (
                            f"{value['column']} points at {target_table} only when "
                            f"{spec['predicate']}; any join through it must carry that "
                            "condition"
                        ),
                        "evidence": finding.evidence,
                    },
                )
            )

    # Suggestions, kept apart from relationships so none of them can reach a query.
    for finding in findings.of(Kind.GAP):
        value = finding.value
        model.gaps.append(
            Gap(
                id=finding.id,
                gap_type=value["gap_type"],
                table=value["table"],
                columns=value["columns"],
                to_table=value["to_table"],
                to_columns=value["to_columns"],
                reason=finding.evidence.get("reason", ""),
                fix=finding.evidence.get("fix", ""),
                blockers=finding.evidence.get("blockers", []),
                evidence={
                    k: v for k, v in finding.evidence.items()
                    if k not in ("reason", "fix", "blockers")
                },
                status=finding.status.value,
                confidence=finding.confidence,
            )
        )

    # ------------------------------------------------- non-schema excavation output
    for finding in findings.of(Kind.ENUM):
        if finding.status is not Status.REJECTED and finding.value:
            model.enums[finding.target] = finding.value

    for finding in findings.of(Kind.DRIFT):
        value = finding.value
        model.drift.append(
            DriftNote(
                table=value["table"], kind=value["kind"], old=value.get("old", ""),
                new=value.get("new", ""), resolution=value.get("resolution", ""),
                boundary=str(value.get("boundary_value") or ""),
                status=finding.status.value, confidence=finding.confidence,
            )
        )

    for finding in findings.of(Kind.EMBEDDED_FORMAT):
        value = finding.value
        model.embedded_formats.append(
            EmbeddedFormat(
                table=value["table"], column=value["column"], format=value["format"],
                fields=value["fields"], status=finding.status.value,
                confidence=finding.confidence,
            )
        )

    for finding in findings.of(Kind.GOD_TABLE):
        value = finding.value
        model.god_tables.append(
            GodTable(
                table=value["table"], discriminator=value["discriminator"],
                entities=value["entities"], status=finding.status.value,
                confidence=finding.confidence,
            )
        )

    # --- the value vocabulary, so the model can be used without the profiles
    for entity in model.entities.values():
        table_profile = profiles.get(entity.source)
        if table_profile is None:
            continue
        for column in entity.columns.values():
            if column.status == "rejected" or column.enum:
                continue
            profile = table_profile.column(column.physical_column or column.name)
            if profile is None or profile.numeric:
                continue
            if not 0 < profile.distinct_count <= 200:
                continue
            shape = profile.value_shape
            if shape is not None and shape.avg_length > 48:
                continue          # free text is not a vocabulary
            values = sorted({
                str(v).strip() for v, _ in profile.top_values
                if v is not None and str(v).strip()
            })
            if values:
                model.vocabulary[f"{entity.name}.{column.name}"] = values

    for finding in findings.of(Kind.PERF):
        value = finding.value
        model.perf_hints.append(
            PerfHint(
                table=value["table"], rows=value["rows"],
                unindexed_filter_columns=value["unindexed_filter_columns"],
                suggested_indexes=value.get("suggested_indexes", []),
            )
        )

    return model


def _logical_entity_name_from(suggested: str, taken: set[str]) -> str:
    base = expand(suggested).logical or suggested.lower()
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def _build_entity(
    *,
    name: str,
    table: Table,
    schema: Schema,
    profiles: ProfileSet,
    of_kind,
    keys_by_table: dict[str, Finding],
    discriminator: str,
    row_estimate: int,
    exclude_columns: list[str] | None,
    enriched_names: dict[str, Any] | None = None,
) -> Entity:
    entity = Entity(
        name=name,
        source=table.qualified,
        description="",
        discriminator=discriminator,
        row_estimate=row_estimate,
        status="hypothesis",
        confidence=0.7,
    )

    key_finding = keys_by_table.get(table.qualified.lower())
    if key_finding is not None:
        entity.key = key_finding.value["columns"]
        entity.confidence = key_finding.confidence

    # A god table's discriminator is a filter that defines the entity, not an
    # exclusion the user can lift.
    if discriminator:
        entity.default_filters.append(
            DefaultFilter(
                expr=discriminator,
                reason=(
                    f"{table.name} holds several kinds of record in one table; this "
                    "predicate selects the ones this entity is about"
                ),
                kind="discriminator",
                overridable=False,
                status="accepted",
                confidence=0.9,
            )
        )

    # --- soft deletes
    for finding in _findings_on_table(of_kind, table, Kind.SOFT_DELETE):
        entity.default_filters.append(
            DefaultFilter(
                expr=finding.value["live_predicate"],
                reason=_first_reason(finding),
                kind="soft_delete",
                overridable=True,
                rows_excluded=finding.evidence.get("rows_affected", 0),
                status=finding.status.value,
                confidence=finding.confidence,
            )
        )

    # --- columns
    table_profile = profiles.get(table.qualified)
    taken_columns: set[str] = set()
    context = expand(table.name).readable

    never_used = {c.lower() for c in (exclude_columns or [])}

    # A drifted pair is one concept in two columns. Work out which physical column
    # carries the resolution expression, which one it supersedes, and what the single
    # surviving logical column should be called.
    superseded: dict[str, str] = {}      # superseded physical column -> surviving one
    drift_names: dict[str, str] = {}     # surviving physical column -> logical name
    for finding in _findings_on_table(of_kind, table, Kind.DRIFT):
        value = finding.value
        old_column, new_column = value.get("old", ""), value.get("new", "")
        if not old_column or not new_column:
            continue
        # The resolution lives on the column the finding targets (the "new" one).
        superseded[old_column.lower()] = new_column
        # Name the survivor for the concept: of the two spellings, the one that
        # expands to fewer words is the plain one (`TOT_AMT` -> "total amount",
        # `TOT_AMT_C` -> "total amount c").
        candidates = sorted(
            (old_column, new_column), key=lambda c: len(expand(c).words)
        )
        drift_names[new_column.lower()] = expand(candidates[0]).logical

    for column in table.columns:
        if column.name.lower() in superseded:
            continue        # folded into the surviving column's expression
        # Drop only the columns this partition never populates. A column that is
        # populated for *some* of the partition's rows belongs to it — that is what
        # drift looks like, and dropping those removes the money from the order.
        if column.name.lower() in never_used and column.name not in entity.key and (
            column.name.lower() != discriminator.split("=")[0].strip().lower()
        ):
            continue

        profile = table_profile.column(column.name) if table_profile else None
        target = f"{table.qualified}.{column.name}"

        expression = column.name
        notes: list[str] = []
        confidence = 0.75
        status = "accepted"

        # 1. type normalisation
        type_findings = of_kind(target, Kind.TYPE_MISMATCH)
        for finding in type_findings:
            if "expression" in finding.value:
                expression = finding.value["expression"]
                notes.append(finding.evidence.get("reason", ""))
                confidence = min(confidence, finding.confidence)

        # 2. quality canonicalisation, composed over whatever came out of step 1
        for finding in sorted(
            of_kind(target, Kind.QUALITY),
            key=lambda f: {"whitespace": 0, "null_markers": 1,
                           "case_variants": 2}.get(f.value.get("issue", ""), 9),
        ):
            issue = finding.value.get("issue")
            if issue == "whitespace":
                expression = f"RTRIM(LTRIM({expression}))"
                notes.append(finding.evidence.get("reason", ""))
            elif issue == "synonyms":
                # Without this, GROUP BY splits one country into five rows and each
                # looks like a plausible answer. Applied to the expression built so
                # far, so it composes with the trimming above.
                clusters = finding.value.get("clusters") or {}
                canonical = _canonical_case_over(expression, clusters)
                if canonical:
                    expression = canonical
                    notes.append(finding.evidence.get("reason", ""))
                    status = "hypothesis"
            elif issue == "null_markers":
                markers = finding.evidence.get("markers", [])
                literals = ", ".join(
                    f"'{m.strip().upper()}'" for m in markers if m.strip()
                )
                condition = f"TRIM(COALESCE({expression},'')) = ''"
                if literals:
                    condition += f" OR UPPER(TRIM({expression})) IN ({literals})"
                expression = f"CASE WHEN {condition} THEN NULL ELSE {expression} END"
                notes.append(finding.evidence.get("reason", ""))
            # case variants are not folded into the expression: doing so would change
            # what the column *returns*. It is recorded for the value linker instead.

        # 3. drift resolution replaces the expression — composed over each side's own
        # type normalisation. Taken raw, `COALESCE(rtn_ts, rtn_dt_txt)` merges a
        # timestamp with DD/MM/YYYY text: SQLite shrugs, Postgres refuses, and neither
        # is right. With each side normalised first, the two halves are both dates.
        drift_description = ""
        for finding in of_kind(target, Kind.DRIFT):
            expression = _compose_over_types(
                finding.value["resolution"],
                [finding.value.get("old", ""), finding.value.get("new", "")],
                table, of_kind,
            )
            # The model now knows the unit better than any documentation does: it has
            # just converted to it. Say so, in words the generator will read — or a
            # stale comment ("amount in cents") reaches the prompt through enrichment
            # and the generator divides by 100 a second time.
            if finding.value.get("kind") == "unit_change" and finding.value.get("ratio"):
                drift_description = (
                    f"already converted to the unit of {finding.value['old']}: values "
                    f"stored in {finding.value['new']} are divided by "
                    f"{finding.value['ratio']} here, so use this column as it is"
                )
            notes.append(finding.evidence.get("reasons", [""])[0])
            confidence = min(confidence, finding.confidence)
            status = finding.status.value

        # Order of preference: a drifted pair's agreed name, then whatever enrichment
        # called it, then the deterministic expansion. Enrichment only ever sees the
        # columns expansion could not read, so this does not override a confident
        # expansion with a guess.
        proposed_column = (enriched_names or {}).get(
            f"{table.qualified}.{column.name}".lower(), {}
        ).get("logical_name")
        forced = drift_names.get(column.name.lower()) or (
            proposed_column if proposed_column not in taken_columns else None
        )
        logical = forced if forced and forced not in taken_columns else \
            _logical_column_name(
                column.name, context, taken_columns,
            # Only a single-column key is "the" id. In a junction table keyed by
            # (PLST_ID, PRD_ID) both columns name other entities, and shortening
            # either to `id` would be wrong twice over.
                is_own_key=(
                    len(entity.key) == 1
                    and column.name.lower() == entity.key[0].lower()
                ),
            )
        taken_columns.add(logical)

        enum_findings = of_kind(target, Kind.ENUM)
        described = (enriched_names or {}).get(
            f"{table.qualified}.{column.name}".lower(), {}
        ).get("description") or ""
        entity.columns[logical] = Column(
            name=logical,
            expr=expression,
            data_type=_logical_type(type_findings, profile),
            physical_column=column.name,
            description=drift_description or described,
            enum=enum_findings[0].value if enum_findings else {},
            status=status,
            confidence=confidence,
            note="; ".join(n for n in notes if n)[:400],
            case_insensitive=_case_untrustworthy(
                profile, of_kind(target, Kind.QUALITY), enum_findings
            ),
        )

    # --- embedded formats become ordinary logical columns
    # Enrichment may have named the cryptic paths inside them; prefer those names over
    # `address_c`, which is unusable in a question.
    path_names: dict[str, dict[str, str]] = {}
    for finding in _findings_on_table(of_kind, table, Kind.NAMING):
        named = (finding.value or {}).get("format_paths")
        if named:
            column_name = finding.target.rsplit(".", 1)[-1]
            path_names.setdefault(column_name, {}).update(named)

    for finding in _findings_on_table(of_kind, table, Kind.EMBEDDED_FORMAT):
        source_column = finding.value["column"]
        for spec in finding.value["fields"]:
            if spec.get("python_extract") or not spec.get("expression"):
                continue
            preferred = path_names.get(source_column, {}).get(spec["path"])
            logical = _logical_column_name(
                preferred or spec["logical_name"], context, taken_columns
            )
            taken_columns.add(logical)
            entity.columns[logical] = Column(
                name=logical,
                expr=spec["expression"],
                physical_column=source_column,
                description=(
                    f"extracted from the {finding.value['format']} document in "
                    f"{source_column}"
                ),
                status=finding.status.value,
                confidence=finding.confidence,
                note=f"{finding.value['format']} path {spec['path']}",
            )

    return entity


def _findings_on_table(of_kind, table: Table, kind: Kind) -> list[Finding]:
    """Findings targeted at any column of a table, plus the table itself."""
    out = list(of_kind(table.qualified, kind))
    for column in table.columns:
        out.extend(of_kind(f"{table.qualified}.{column.name}", kind))
    return out


def _canonical_case_over(expression: str, clusters: dict[str, list[str]]) -> str:
    """Fold known spelling variants onto their canonical form, over `expression`."""
    whens: list[str] = []
    for canonical, members in clusters.items():
        for member in members:
            if member == canonical:
                continue
            literal = str(member).replace("'", "''").upper()
            target = str(canonical).replace("'", "''")
            whens.append(f"WHEN '{literal}' THEN '{target}'")
    if not whens:
        return ""
    return f"CASE UPPER(TRIM({expression})) {' '.join(whens)} ELSE {expression} END"


def _logical_for_physical(entity, physical_column: str) -> str:
    """The logical column that projects this physical one, if the entity has one."""
    lowered = (physical_column or "").lower()
    for column in entity.columns.values():
        if column.physical_column.lower() == lowered:
            return column.name
    return ""


def _first_reason(finding: Finding) -> str:
    reasons = finding.evidence.get("reasons")
    if isinstance(reasons, list) and reasons:
        return reasons[0]
    return finding.evidence.get("reason", "")


def _logical_type(type_findings: list[Finding], profile) -> str:
    for finding in type_findings:
        logical = finding.value.get("logical_type")
        if logical:
            return logical
    if profile is not None and profile.numeric:
        return "number"
    return "text"
