"""The typed output every detector returns.

CLAUDE.md conventions: each detector is a pure function returning findings that carry
`evidence` and `confidence`. Evidence is what a human sees in the Workbench when they
are deciding whether to accept a hypothesis, so it has to be concrete — value
distributions, sample rows, the procedure text a rule came from — not prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """What a finding claims. One per detector family."""

    TABLE_CLASS = "table_class"            # core / archive / staging / backup / dead
    JOIN = "join"                          # an undeclared relationship
    KEY = "key"                            # a logical or composite key
    ENUM = "enum"                          # a code column and its decoded labels
    SOFT_DELETE = "soft_delete"            # a default filter for an entity
    EMBEDDED_FORMAT = "embedded_format"    # XML / JSON / k=v inside a column
    OVERLOADED = "overloaded"              # meaning depends on a sibling discriminator
    GOD_TABLE = "god_table"                # several entities sharing one table
    DRIFT = "drift"                        # the schema changed under the data
    TYPE_MISMATCH = "type_mismatch"        # same meaning, different storage
    QUALITY = "quality"                    # padding, null markers, case, synonyms
    PERF = "perf"                          # cost-guard hint
    NAMING = "naming"                      # a logical name for a cryptic one
    DESCRIPTION = "description"            # a one-line description of an object
    JOIN_REFERENT = "join_referent"        # which parent a key points at, when values tie
    GAP = "gap"                            # something that should exist and does not


class Status(StrEnum):
    HYPOTHESIS = "hypothesis"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class Finding:
    kind: Kind
    target: str                 # "TABLE" or "TABLE.COLUMN" this is about
    value: Any                  # the claim, shaped per kind
    confidence: float           # 0..1
    evidence: dict[str, Any] = field(default_factory=dict)
    detector: str = ""
    status: Status = Status.HYPOTHESIS
    note: str = ""

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        # A finding the detector is certain about does not need a human.
        if self.confidence >= 0.95 and self.status is Status.HYPOTHESIS:
            self.status = Status.ACCEPTED

    @property
    def id(self) -> str:
        return f"{self.kind.value}:{self.target}:{_stable(self.value)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "target": self.target,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "detector": self.detector,
            "status": self.status.value,
            "note": self.note,
        }


def _stable(value: Any) -> str:
    if isinstance(value, dict):
        return ",".join(f"{k}={_stable(v)}" for k, v in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return "+".join(_stable(v) for v in value)
    return str(value)


@dataclass
class FindingSet:
    """Everything the detectors produced, with the bookkeeping the review queue needs."""

    findings: list[Finding] = field(default_factory=list)

    def add(self, *findings: Finding) -> None:
        self.findings.extend(f for f in findings if f is not None)

    def extend(self, findings) -> None:
        self.findings.extend(findings)

    def of(self, kind: Kind) -> list[Finding]:
        return [f for f in self.findings if f.kind is kind]

    def about(self, target: str) -> list[Finding]:
        lowered = target.lower()
        return [
            f for f in self.findings
            if f.target.lower() == lowered or f.target.lower().startswith(lowered + ".")
        ]

    def by_detector(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.detector] = out.get(f.detector, 0) + 1
        return dict(sorted(out.items()))

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)


def merge_confidence(*values: float) -> float:
    """Combine independent pieces of evidence that all point the same way.

    Noisy-OR: each additional signal closes part of the remaining gap to 1.0, so two
    weak signals beat one weak signal but never reach certainty on their own.
    """
    remaining = 1.0
    for value in values:
        remaining *= 1.0 - max(0.0, min(1.0, value))
    return 1.0 - remaining
