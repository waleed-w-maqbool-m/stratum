"""Persistence for semantic models and the review queue.

Models are versioned YAML on disk. Versioning matters because the Workbench edits a
model that queries are already running against: an accepted hypothesis is a new version,
and a run records which version answered it so a regression can be traced to the change
that caused it.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import SemanticModel


@dataclass
class ReviewItem:
    """One hypothesis a human should look at, ordered by how much rides on it."""

    id: str
    kind: str
    target: str
    value: Any
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    detector: str = ""
    status: str = "hypothesis"
    impact: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "target": self.target,
            "value": self.value, "confidence": round(self.confidence, 3),
            "evidence": self.evidence, "detector": self.detector,
            "status": self.status, "impact": round(self.impact, 3), "note": self.note,
        }


class ModelStore:
    """Versioned semantic models under one directory per database."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, database: str) -> Path:
        path = self.root / database
        path.mkdir(parents=True, exist_ok=True)
        return path

    def versions(self, database: str) -> list[int]:
        return sorted(
            int(p.stem.split("v")[-1])
            for p in self._dir(database).glob("model.v*.yaml")
        )

    def latest_version(self, database: str) -> int:
        versions = self.versions(database)
        return versions[-1] if versions else 0

    def save(self, model: SemanticModel) -> int:
        version = self.latest_version(model.database) + 1
        model.version = version
        directory = self._dir(model.database)
        (directory / f"model.v{version}.yaml").write_text(model.to_yaml(), "utf-8")
        # `current` is a copy rather than a symlink: Windows needs a privilege for
        # symlinks that a normal user does not have.
        shutil.copyfile(
            directory / f"model.v{version}.yaml", directory / "current.yaml"
        )
        return version

    def load(self, database: str, version: int | None = None) -> SemanticModel | None:
        directory = self._dir(database)
        path = (
            directory / f"model.v{version}.yaml" if version
            else directory / "current.yaml"
        )
        if not path.exists():
            return None
        return SemanticModel.from_yaml(path.read_text("utf-8"))

    def save_findings(self, database: str, findings: list[dict[str, Any]]) -> None:
        (self._dir(database) / "findings.json").write_text(
            json.dumps(findings, indent=2, default=str), "utf-8"
        )

    def save_catalog(self, database: str, catalog: dict[str, Any]) -> None:
        (self._dir(database) / "catalog.json").write_text(
            json.dumps(catalog, indent=1, default=str), "utf-8"
        )

    def load_catalog(self, database: str) -> dict[str, Any] | None:
        path = self._dir(database) / "catalog.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def load_findings(self, database: str) -> list[dict[str, Any]]:
        path = self._dir(database) / "findings.json"
        if not path.exists():
            return []
        return json.loads(path.read_text("utf-8"))

    def verified_path(self, database: str) -> Path:
        return self._dir(database) / "verified.json"

    def cache_path(self, database: str) -> Path:
        """Where the semantic cache for this database lives."""
        return self._dir(database) / "semantic-cache.json"

    def cache_dir(self, database: str) -> Path:
        path = self._dir(database) / "llm-cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def record_run(self, database: str, run: dict[str, Any]) -> None:
        directory = self._dir(database) / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (directory / f"{stamp}.json").write_text(
            json.dumps(run, indent=2, default=str), "utf-8"
        )


def build_review_queue(
    findings: list, model: SemanticModel, limit: int = 200
) -> list[ReviewItem]:
    """Order hypotheses by impact: uncertainty times how much depends on them.

    PLAN.md 4.2 step 5. A soft-delete rule on the orders table changes every revenue
    answer, so it outranks a description on a lookup table even if both are equally
    uncertain. Getting this order right is what makes the Workbench worth opening.
    """
    # How many relationships touch each table — a proxy for how central it is.
    centrality: dict[str, int] = {}
    for relationship in model.relationships:
        for table in (relationship.from_table, relationship.to_table):
            if table:
                centrality[table.lower()] = centrality.get(table.lower(), 0) + 1

    rows = {t.lower(): e.row_estimate for t in model.entities for e in [model.entities[t]]}

    kind_weight = {
        "soft_delete": 1.0,     # changes the row set of every query on the entity
        "god_table": 0.95,      # defines what an entity even is
        "join": 0.9,            # a wrong join is silently wrong
        "gap": 0.35,            # a suggestion; nothing depends on it until accepted
        "drift": 0.85,          # silently halves a measure
        "key": 0.7,             # fan-out
        "overloaded": 0.7,
        "enum": 0.5,            # affects filters that name a label
        "embedded_format": 0.45,
        "type_mismatch": 0.4,
        "quality": 0.3,
        "naming": 0.25,
        "table_class": 0.2,
        "perf": 0.15,
        "description": 0.1,
    }

    items: list[ReviewItem] = []
    for finding in findings:
        data = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding)
        if data.get("status") != "hypothesis":
            continue
        target_table = data["target"].split(".")[0].lower()
        uncertainty = 1.0 - float(data.get("confidence", 0.5))
        weight = kind_weight.get(data["kind"], 0.3)
        # Log rows so a 250k-row audit table does not swamp everything else.
        import math

        size = math.log10(max(10, rows.get(target_table, 10)))
        links = 1 + centrality.get(target_table, 0)
        impact = uncertainty * weight * size * math.log2(1 + links)

        items.append(
            ReviewItem(
                id=data["id"],
                kind=data["kind"],
                target=data["target"],
                value=data["value"],
                confidence=float(data.get("confidence", 0.5)),
                evidence=data.get("evidence", {}),
                detector=data.get("detector", ""),
                impact=impact,
                note=data.get("note", ""),
            )
        )

    items.sort(key=lambda item: -item.impact)
    return items[:limit]
