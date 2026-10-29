"""The excavation pipeline: introspect -> profile -> detect -> compile.

Ordering is not arbitrary. Several detectors depend on what an earlier one found:

* `artifacts` runs early so every later detector can ignore the 110 dead tables.
* `procmine` runs before `joins`, `enums` and `softdelete`, which all take what
  shipped SQL said as a prior rather than re-deriving it statistically.
* `godtable` runs before `joins`, because a god table's composite key is not unique
  until the discriminator is applied.

Every stage emits a trace event, which the SSE endpoint and the eval harness both
consume (CLAUDE.md conventions).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..connectors.base import Connector
from ..connectors.executor import ReadOnlyExecutor
from .findings import FindingSet, Kind
from .introspect import Schema, introspect
from .profile import ProfileSet, profile_schema


@dataclass
class TraceEvent:
    stage: str
    ms: int
    tokens: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "ms": self.ms, "tokens": self.tokens,
                "detail": self.detail}


@dataclass
class ExcavationResult:
    schema: Schema
    profiles: ProfileSet
    findings: FindingSet
    trace: list[TraceEvent] = field(default_factory=list)
    table_classes: dict[str, str] = field(default_factory=dict)
    discriminators: dict[str, str] = field(default_factory=dict)
    mined: Any = None

    def summary(self) -> dict[str, Any]:
        return {
            **self.schema.summary(),
            "findings": len(self.findings),
            "by_detector": self.findings.by_detector(),
            "total_ms": sum(e.ms for e in self.trace),
            "total_tokens": sum(e.tokens for e in self.trace),
        }


Emit = Callable[[TraceEvent], None]


def excavate(
    connector: Connector,
    *,
    sample_size: int = 2000,
    profile_budget_s: float = 180.0,
    emit: Emit | None = None,
    llm=None,
) -> ExcavationResult:
    from . import (
        artifacts,
        declared,
        drift,
        enums,
        formats,
        gaps,
        godtable,
        joins,
        keys,
        overloaded,
        perf,
        procmine,
        quality,
        softdelete,
        types,
    )

    trace: list[TraceEvent] = []

    def stage(name: str):
        started = time.perf_counter()

        def done(**detail: Any) -> None:
            event = TraceEvent(
                stage=name, ms=int((time.perf_counter() - started) * 1000), detail=detail
            )
            trace.append(event)
            if emit is not None:
                emit(event)

        return done

    executor = ReadOnlyExecutor(connector, connector.profile)
    findings = FindingSet()

    done = stage("introspect")
    schema = introspect(connector)
    done(**schema.summary())

    # Every probe below is written from catalog names. On a dialect that folds unquoted
    # names, the mixed-case ones have to be quoted or the probe fails — and a failed
    # probe reads as "no evidence", so a join into `tblFilm` would simply not be found.
    from ..ask.dialect import needs_quoting

    executor.quoted_identifiers = {
        name
        for table in schema.tables.values()
        for name in (table.name, *(c.name for c in table.columns))
        if needs_quoting(name, schema.dialect)
    }

    # What the catalog declares is read before anything is inferred, and is only ever
    # confirmed afterwards, never re-derived.
    done = stage("declared")
    declared_findings = declared.detect(schema)
    done(foreign_keys=len(declared_findings),
         not_enforced=sum(
             1 for f in declared_findings if f.evidence.get("enforced") is False
         ))

    done = stage("profile")
    profiles = profile_schema(
        executor, schema, sample_size=sample_size, time_budget_s=profile_budget_s
    )
    done(tables=len(profiles.tables), skipped=len(profiles.skipped))

    done = stage("artifacts")
    artifact_findings = artifacts.detect(schema, profiles)
    findings.extend(artifact_findings)
    classes = artifacts.classification_map(artifact_findings)
    done(**{
        "classified": len(classes),
        "core": sum(1 for v in classes.values() if v == "core"),
        "excluded": sum(1 for v in classes.values() if v != "core"),
    })

    done = stage("procmine")
    mined_findings, mined = procmine.detect(schema)
    # Its join findings are left out: `joins` re-emits every mined join with the right
    # parent and child, the routines that wrote it and a measurement against the data.
    # Keeping procmine's copy gave each such edge twice, sometimes backwards.
    findings.extend(f for f in mined_findings if f.kind is not Kind.JOIN)
    done(routines_parsed=mined.parsed, joins=len(mined.joins), enums=len(mined.enums),
         filters=len(mined.filters))

    done = stage("godtable")
    god_findings = godtable.detect(executor, schema, profiles, classes)
    findings.extend(god_findings)
    discriminators = godtable.discriminator_map(god_findings)
    done(god_tables=len(god_findings))

    done = stage("joins")
    rejected_joins: list[joins.Rejected] = []
    join_findings = joins.detect(
        executor, schema, profiles, mined, classes, discriminators,
        declared=declared_findings, rejected=rejected_joins,
    )
    findings.extend(join_findings)
    by_origin: dict[str, int] = {}
    for f in join_findings:
        origin = f.evidence.get("origin", "data")
        by_origin[origin] = by_origin.get(origin, 0) + 1
    done(relationships=len(join_findings), **{f"from_{k}": v for k, v in by_origin.items()},
         orphaned=sum(1 for f in join_findings if f.evidence.get("orphan_count")))

    done = stage("keys")
    key_findings = keys.detect(
        executor, schema, profiles, classes, discriminators, join_findings
    )
    findings.extend(key_findings)
    done(keys=len(key_findings))

    done = stage("softdelete")
    sd_findings = softdelete.detect(executor, schema, profiles, mined, classes)
    findings.extend(sd_findings)
    done(rules=len(sd_findings))

    done = stage("enums")
    enum_findings = enums.detect(
        schema, profiles, mined, classes, join_findings, executor
    )
    findings.extend(enum_findings)
    done(enums=len(enum_findings))

    done = stage("formats")
    format_findings = formats.detect(schema, profiles, connector.profile, classes)
    findings.extend(format_findings)
    done(columns=len(format_findings))

    done = stage("overloaded")
    overloaded_findings = overloaded.detect(
        executor, schema, profiles, classes, join_findings
    )
    findings.extend(overloaded_findings)
    done(columns=len(overloaded_findings))

    done = stage("drift")
    drift_findings = drift.detect(executor, schema, profiles, classes, discriminators)
    drift_findings += drift.detect_relocation(
        executor, schema, profiles, join_findings, classes
    )
    findings.extend(drift_findings)
    done(drifts=len(drift_findings))

    done = stage("types")
    type_findings = types.detect(schema, profiles, classes)
    findings.extend(type_findings)
    done(mismatches=len(type_findings))

    done = stage("quality")
    quality_findings = quality.detect(schema, profiles, classes)
    findings.extend(quality_findings)
    done(issues=len(quality_findings))

    done = stage("perf")
    perf_findings = perf.detect(schema, profiles, classes)
    findings.extend(perf_findings)
    done(traps=len(perf_findings))

    # Enrichment is last and optional: it can only add meaning to what the detectors
    # already found, and everything below it works without an LLM at all.
    if llm is not None and getattr(llm, "available", False):
        from . import enrich as enrich_module

        done = stage("enrich")
        before = len(llm.calls)
        enrich_started = time.perf_counter()
        enriched = enrich_module.enrich(
            llm, schema, profiles, list(findings), classes
        )
        enrich_module.merge_enum_findings(findings.findings, enriched)
        # Repoint any relationship the data could not decide, before the model is
        # compiled from these findings.
        enrich_module.merge_referent_findings(findings.findings, enriched)
        findings.extend([
            f for f in enriched
            if f.kind not in (Kind.ENUM, Kind.JOIN_REFERENT)
        ])

        # Value spellings string similarity cannot reach: 'United States' and 'USA'
        # share almost no characters, and grouping on the raw column splits a country
        # into five rows that each look like an answer.
        value_findings = enrich_module.enrich_values(
            llm, profiles, list(findings)
        )
        enrich_module.merge_value_findings(findings.findings, value_findings)
        tokens = sum(c.tokens for c in llm.calls[before:])
        event = TraceEvent(
            stage="enrich", ms=int((time.perf_counter() - enrich_started) * 1000), tokens=tokens,
            detail={"findings": len(enriched), "calls": len(llm.calls) - before},
        )
        trace.append(event)
        if emit is not None:
            emit(event)

    # Last, so it sees the relationships and enum decodings after any model-assisted
    # repointing. It only suggests; nothing it finds is used until a reviewer accepts it.
    done = stage("gaps")
    gap_findings = gaps.detect(
        executor, schema, profiles, join_findings, rejected_joins, classes, enum_findings
    )
    findings.extend(gap_findings)
    by_type: dict[str, int] = {}
    for g in gap_findings:
        by_type[g.value["gap_type"]] = by_type.get(g.value["gap_type"], 0) + 1
    done(gaps=len(gap_findings), **by_type)

    return ExcavationResult(
        schema=schema,
        profiles=profiles,
        findings=findings,
        trace=trace,
        table_classes=classes,
        discriminators=discriminators,
        mined=mined,
    )
