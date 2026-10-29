"""A plain sentence for why a relationship is believed.

Deterministic and language-model-free: the reasons a person reads next to a join are
built from the numbers the detectors measured, not written by a model, so they cannot
drift from the evidence.
"""

from __future__ import annotations

from typing import Any

from ..origin import Origin


def _pct(value: float) -> str:
    text = f"{value * 100:.1f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _side(table: str, columns: list[str]) -> str:
    short = table.rsplit(".", 1)[-1]
    return ", ".join(f"{short}.{c}" for c in columns) or short


def _routines(sources: list[str]) -> str:
    if not sources:
        return "shipped SQL"
    shown = ", ".join(sources[:3])
    return shown + (f" and {len(sources) - 3} more" if len(sources) > 3 else "")


def _measurement(evidence: dict[str, Any], parent: str) -> str:
    """The data check, when one ran."""
    if not evidence.get("inclusion_measured"):
        return ""
    tested = evidence.get("distinct_values_tested") or 0
    inclusion = evidence.get("inclusion") or 0.0
    orphans = evidence.get("orphan_count") or 0
    text = f"{_pct(inclusion)} of {tested} distinct values tested exist in {parent}"
    if orphans:
        samples = evidence.get("orphan_samples") or []
        found = f" (for example {', '.join(samples)})" if samples else ""
        text += f"; {orphans} {'has' if orphans == 1 else 'have'} no match{found}"
    return text + "."


def describe(
    origin: str,
    evidence: dict[str, Any],
    from_table: str,
    from_columns: list[str],
    to_table: str,
    to_columns: list[str],
    condition: str = "",
) -> str:
    parent = to_table.rsplit(".", 1)[-1]
    evidence = evidence or {}
    measured = _measurement(evidence, parent)
    corroborated = evidence.get("corroborated_by") or []

    if origin == Origin.DECLARED.value:
        name = evidence.get("fk_name")
        text = f"Declared as foreign key {name}." if name else "Declared as a foreign key."
        if evidence.get("enforced") is False:
            text += " The constraint exists but the database is not enforcing it."
        if Origin.SHIPPED_SQL.value in corroborated:
            text += " Views or procedures also join on it."
        return " ".join(part for part in (text, measured) if part)

    if origin == Origin.SHIPPED_SQL.value:
        text = f"Not declared. Written as a join in {_routines(evidence.get('sources') or [])}."
        return " ".join(part for part in (text, measured) if part)

    if origin == Origin.CONDITIONAL.value:
        text = (
            f"{_side(from_table, from_columns)} points at {parent} only when {condition}."
            if condition else
            f"{_side(from_table, from_columns)} points at {parent} for some rows only."
        )
        return text

    if origin == Origin.LLM.value:
        return "Guessed by the language model from column names; not tested against the data."

    parts = ["Not declared."]
    if measured:
        parts.append(measured)
    if evidence.get("parent_key_is_unique"):
        parts.append(f"{_side(to_table, to_columns)} is unique.")
    similarity = evidence.get("name_similarity")
    if similarity is not None:
        parts.append(f"The names match at {similarity:.2f}.")
    if evidence.get("needs_cast"):
        parts.append("The two sides are stored as different types, so the join casts one.")
    if evidence.get("chosen_by") == "llm":
        was = evidence.get("measured_against") or "another table"
        parts.append(
            f"The values fit more than one table equally well; a language model chose "
            f"{parent} over {was}, so the numbers above are for {was}."
        )
    return " ".join(parts)
