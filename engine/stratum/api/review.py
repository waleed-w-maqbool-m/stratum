"""Applying a human's decision to the semantic model.

The Workbench is the point of the confidence scores: a person accepts, edits or rejects
a hypothesis and the model changes. Every action produces a new model version, so a
regression in the next eval run can be traced to the decision that caused it.

Accepting is not merely a status flip. Accepting a soft-delete rule makes its filter
authoritative, which raises the confidence of every future answer over that entity;
rejecting one removes the filter and the rows come back. Both are recorded on the
finding as well as on the model so the review queue does not re-ask.
"""

from __future__ import annotations

from typing import Any

from ..semantic.model import SemanticModel


def apply_review_action(
    model: SemanticModel,
    findings: list[dict[str, Any]],
    item_id: str,
    action: str,
    value: Any = None,
) -> bool:
    """Apply one decision. Returns False when the item is unknown."""
    finding = next((f for f in findings if f.get("id") == item_id), None)
    if finding is None:
        return False

    if action not in ("accept", "reject", "edit"):
        raise ValueError(f"unknown action {action!r}")

    finding["status"] = "accepted" if action in ("accept", "edit") else "rejected"
    if action == "edit" and value is not None:
        finding["value"] = value

    kind = finding["kind"]
    target = finding["target"]
    new_value = finding["value"]
    status = finding["status"]

    if kind == "soft_delete":
        _apply_soft_delete(model, new_value, status)
    elif kind == "join":
        _apply_join(model, new_value, status)
    elif kind == "enum":
        _apply_enum(model, target, new_value, status)
    elif kind in ("type_mismatch", "quality", "drift"):
        _apply_column_expression(model, target, new_value, status)
    elif kind == "key":
        _apply_key(model, target, new_value, status)
    elif kind == "naming":
        _apply_naming(model, target, new_value, status)
    elif kind == "table_class":
        model.table_classes[target] = new_value
    return True


def _entity_for_source(model: SemanticModel, source: str):
    lowered = source.lower()
    return [e for e in model.entities.values() if e.source.lower() == lowered]


def _apply_soft_delete(model: SemanticModel, value: dict, status: str) -> None:
    for entity in _entity_for_source(model, value.get("table", "")):
        for filt in entity.default_filters:
            if filt.expr == value.get("live_predicate"):
                filt.status = status
                filt.confidence = 1.0 if status == "accepted" else 0.0


def _apply_join(model: SemanticModel, value: dict, status: str) -> None:
    for relationship in model.relationships:
        if (
            relationship.from_table.lower() == value.get("from_table", "").lower()
            and [c.lower() for c in relationship.from_columns]
            == [c.lower() for c in value.get("from_columns", [])]
        ):
            relationship.status = status
            if status == "accepted":
                relationship.confidence = 1.0


def _apply_enum(model: SemanticModel, target: str, value: dict, status: str) -> None:
    if status == "rejected":
        model.enums.pop(target, None)
    else:
        model.enums[target] = value

    table, _, column = target.rpartition(".")
    for entity in _entity_for_source(model, table):
        for logical in entity.columns.values():
            if logical.physical_column.lower() == column.lower():
                logical.enum = {} if status == "rejected" else value
                if status == "accepted":
                    logical.status = "accepted"


def _apply_column_expression(
    model: SemanticModel, target: str, value: dict, status: str
) -> None:
    table, _, column = target.rpartition(".")
    expression = value.get("expression") or value.get("resolution")
    for entity in _entity_for_source(model, table):
        for logical in entity.columns.values():
            if logical.physical_column.lower() != column.lower():
                continue
            logical.status = status
            if status == "accepted":
                logical.confidence = 1.0
                if expression:
                    logical.expr = expression
            elif status == "rejected":
                # Fall back to the raw column rather than leaving a rejected expression
                # in place: the user is saying the normalisation is wrong, not that the
                # column does not exist.
                logical.expr = logical.physical_column or logical.expr
                logical.confidence = 0.4


def _apply_key(model: SemanticModel, target: str, value: dict, status: str) -> None:
    for entity in _entity_for_source(model, target):
        if status == "accepted":
            entity.key = value.get("columns", entity.key)
            entity.confidence = 1.0
        else:
            entity.confidence = min(entity.confidence, 0.4)


def _apply_naming(
    model: SemanticModel, target: str, value: dict, status: str
) -> None:
    if status != "accepted":
        return
    logical_name = value.get("logical_name")
    description = value.get("description")

    if "." not in target:
        for entity in _entity_for_source(model, target):
            if description:
                entity.description = description
            if logical_name and logical_name not in model.entities:
                model.entities.pop(entity.name, None)
                entity.name = logical_name
                model.entities[logical_name] = entity
        return

    table, _, column = target.rpartition(".")
    for entity in _entity_for_source(model, table):
        for key, logical in list(entity.columns.items()):
            if logical.physical_column.lower() != column.lower():
                continue
            if description:
                logical.description = description
            if logical_name and logical_name not in entity.columns:
                entity.columns.pop(key, None)
                logical.name = logical_name
                entity.columns[logical_name] = logical
            logical.status = "accepted"
