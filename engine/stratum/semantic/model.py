"""The semantic model: a clean logical schema over a messy physical one.

This is the artefact the whole system turns on. The LLM writes SQL against the logical
names defined here — `customer.first_name`, `order.total_amount` — and the compiler
rewrites that into the physical reality: `RTRIM(CFNM1)`, a CASE that divides by 100
after the cents cutover, a soft-delete filter, a discriminator on a god table.

Legacy mess is handled once, here, in something a human can read and correct. It is not
re-guessed by a language model on every question.

Every item carries `status` and `confidence`, because most of this was inferred and
some of it is wrong. The Workbench exists to let a person fix the ones that are.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ..origin import derive_origin

Status = Literal["hypothesis", "accepted", "rejected"]


class Provenance(BaseModel):
    """Why the model believes something, kept alongside the claim itself."""

    detector: str = ""
    reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class Column(BaseModel):
    """One logical column and the physical expression that produces it."""

    name: str
    expr: str                                  # SQL over the source table's columns
    data_type: str = "text"
    description: str = ""
    physical_column: str = ""                  # the raw column, when there is just one
    enum: dict[str, str] = Field(default_factory=dict)
    synonyms: list[str] = Field(default_factory=list)
    status: Status = "hypothesis"
    confidence: float = 0.5
    note: str = ""
    provenance: Provenance | None = None
    # Stored text whose case cannot be trusted to match what a person types: all
    # capitals (a mainframe load), or the same value in several cases. The compiler
    # compares it against literals case-insensitively.
    case_insensitive: bool = False

    @property
    def is_derived(self) -> bool:
        return self.expr.strip().lower() != (self.physical_column or "").strip().lower()


class DefaultFilter(BaseModel):
    """A predicate applied to every query against an entity unless overridden.

    Soft deletes and god-table discriminators both land here. `overridable` says
    whether a user can ask to see the excluded rows — a discriminator cannot be
    dropped without changing what the entity *is*, but a soft-delete filter can.
    """

    expr: str
    reason: str
    kind: Literal["soft_delete", "discriminator", "other"] = "other"
    overridable: bool = True
    rows_excluded: int = 0
    status: Status = "hypothesis"
    confidence: float = 0.5


class Entity(BaseModel):
    """A logical thing, backed by one physical table (or part of one)."""

    name: str
    source: str                                 # physical table
    key: list[str] = Field(default_factory=list)
    description: str = ""
    discriminator: str = ""                     # god-table split predicate
    default_filters: list[DefaultFilter] = Field(default_factory=list)
    columns: dict[str, Column] = Field(default_factory=dict)
    synonyms: list[str] = Field(default_factory=list)
    row_estimate: int = 0
    status: Status = "hypothesis"
    confidence: float = 0.5

    def column(self, name: str) -> Column | None:
        return self.columns.get(name.lower())

    def active_filters(self) -> list[DefaultFilter]:
        return [f for f in self.default_filters if f.status != "rejected"]


class Relationship(BaseModel):
    """A join between two entities, and where the belief in it came from.

    `origin` says whether the catalog declares it, shipped SQL writes it, the data implies
    it, or a language model chose it; `corroborated_by` lists the other origins that found
    the same edge. Models saved before these fields existed derive them on load.
    """

    name: str
    from_entity: str
    to_entity: str
    from_table: str
    to_table: str
    from_columns: list[str] = Field(default_factory=list)
    to_columns: list[str] = Field(default_factory=list)
    on: str = ""                                # the physical join expression, with casts
    logical_on: str = ""                        # the same join in logical column names
    condition: str = ""                         # extra predicate for overloaded columns
    cardinality: Literal["many_to_one", "one_to_one", "one_to_many"] = "many_to_one"
    optional: bool = False                      # the child column is sometimes null
    origin: str = ""
    corroborated_by: list[str] = Field(default_factory=list)
    status: Status = "hypothesis"
    confidence: float = 0.5
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _fill_origin(self) -> Relationship:
        if not self.origin:
            detector = self.provenance.detector if self.provenance else ""
            evidence = self.provenance.evidence if self.provenance else {}
            self.origin = derive_origin(detector, evidence)
        return self


class Metric(BaseModel):
    name: str
    expr: str
    filters: list[str] = Field(default_factory=list)
    description: str = ""
    status: Status = "hypothesis"
    confidence: float = 0.5


class DriftNote(BaseModel):
    table: str
    kind: str
    old: str = ""
    new: str = ""
    resolution: str = ""
    boundary: str = ""
    status: Status = "hypothesis"
    confidence: float = 0.5


class EmbeddedFormat(BaseModel):
    table: str
    column: str
    format: str
    fields: list[dict[str, Any]] = Field(default_factory=list)
    status: Status = "hypothesis"
    confidence: float = 0.5


class GodTable(BaseModel):
    table: str
    discriminator: str
    entities: dict[str, Any] = Field(default_factory=dict)
    status: Status = "hypothesis"
    confidence: float = 0.5


class PerfHint(BaseModel):
    table: str
    rows: int
    unindexed_filter_columns: list[str] = Field(default_factory=list)
    suggested_indexes: list[str] = Field(default_factory=list)


class SemanticModel(BaseModel):
    version: int = 1
    database: str
    dialect: str = "sqlite"
    profile: str = "sqlite3"

    entities: dict[str, Entity] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    metrics: dict[str, Metric] = Field(default_factory=dict)
    glossary: dict[str, str] = Field(default_factory=dict)

    # Excavation output that is not itself part of the logical schema but which the
    # pipeline, the Workbench and the knowledge-recall score all read.
    table_classes: dict[str, str] = Field(default_factory=dict)
    enums: dict[str, dict[str, str]] = Field(default_factory=dict)
    # {entity.column: [stored values]} for the columns small enough to enumerate.
    # Carried in the model so value linking works from a model loaded off disk.
    vocabulary: dict[str, list[str]] = Field(default_factory=dict)
    drift: list[DriftNote] = Field(default_factory=list)
    embedded_formats: list[EmbeddedFormat] = Field(default_factory=list)
    god_tables: list[GodTable] = Field(default_factory=list)
    perf_hints: list[PerfHint] = Field(default_factory=list)
    # Physical names that only resolve when quoted in this dialect — on Postgres, any
    # name that is not all lowercase. Every fragment in the model is written with the
    # catalog's exact spelling; the compiler quotes these on the way out.
    quoted_identifiers: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------ lookups
    def entity(self, name: str) -> Entity | None:
        return self.entities.get(name.lower())

    def entity_for_table(self, table: str) -> Entity | None:
        lowered = table.lower()
        return next(
            (e for e in self.entities.values() if e.source.lower() == lowered), None
        )

    def relationships_for(self, entity: str) -> list[Relationship]:
        lowered = entity.lower()
        return [
            r for r in self.relationships
            if lowered in (r.from_entity.lower(), r.to_entity.lower())
            and r.status != "rejected"
        ]

    def accepted_entities(self) -> list[Entity]:
        return [e for e in self.entities.values() if e.status != "rejected"]

    # ------------------------------------------------------------ serialisation
    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_defaults=False),
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )

    @classmethod
    def from_yaml(cls, text: str) -> SemanticModel:
        return cls.model_validate(yaml.safe_load(text))

    def to_scoring_dict(self) -> dict[str, Any]:
        """The shape `gauntlet/scorer.py` compares against the manifest.

        Deliberately a flat, explicit projection rather than the full model dump: the
        scorer must not have to understand the model's internals, and the model must
        not be shaped by what the scorer wants.
        """
        return {
            "entities": [
                {
                    "name": e.name,
                    "source": e.source,
                    "key": e.key,
                    "default_filters": [
                        f.expr for f in e.default_filters if f.status != "rejected"
                    ],
                }
                for e in self.accepted_entities()
            ],
            "relationships": [
                {
                    "from_table": r.from_table,
                    "from_columns": r.from_columns,
                    "to_table": r.to_table,
                    "to_columns": r.to_columns,
                    "confidence": r.confidence,
                }
                for r in self.relationships
                if r.status != "rejected"
            ],
            "table_classes": self.table_classes,
            "enums": self.enums,
            "drift": [
                {"table": d.table, "kind": d.kind, "old": d.old, "new": d.new}
                for d in self.drift
            ],
            "embedded_formats": [
                {"table": f.table, "column": f.column, "format": f.format}
                for f in self.embedded_formats
            ],
            "god_tables": [
                {"table": g.table, "discriminator": g.discriminator}
                for g in self.god_tables
            ],
        }

    def stats(self) -> dict[str, Any]:
        columns = sum(len(e.columns) for e in self.entities.values())
        hypotheses = sum(
            1 for e in self.entities.values()
            for c in e.columns.values() if c.status == "hypothesis"
        ) + sum(1 for r in self.relationships if r.status == "hypothesis")
        return {
            "entities": len(self.entities),
            "columns": columns,
            "relationships": len(self.relationships),
            "metrics": len(self.metrics),
            "open_hypotheses": hypotheses,
            "enums": len(self.enums),
        }
