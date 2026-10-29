"""Where a relationship came from.

Kept free of imports so the semantic model, the detectors and the API can all use it
without a cycle.

Four real sources exist and they differ in how much to trust them:

* `declared`    the database's own catalog states it as a foreign key
* `shipped_sql` written as a join in a view, procedure or function that works today
* `data`        not written anywhere; the values of two columns line up
* `llm`         a language model chose between parents the data could not separate

`conditional` is a join that holds only for some rows of an overloaded column. Its
strength is whatever the evidence behind it was; the label says it needs a predicate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Origin(StrEnum):
    DECLARED = "declared"
    SHIPPED_SQL = "shipped_sql"
    DATA = "data"
    LLM = "llm"
    CONDITIONAL = "conditional"


# Strongest first. Used to pick which origin names an edge that several found.
RANK = [Origin.DECLARED, Origin.SHIPPED_SQL, Origin.DATA, Origin.LLM, Origin.CONDITIONAL]


def strongest(origins: list[str]) -> str:
    order = {o.value: i for i, o in enumerate(RANK)}
    return min(origins, key=lambda o: order.get(o, len(order)))


def derive_origin(detector: str = "", evidence: dict[str, Any] | None = None) -> str:
    """Work out the origin of a relationship saved before `origin` existed."""
    evidence = evidence or {}
    if evidence.get("origin"):
        return str(evidence["origin"])
    if detector == "declared":
        return Origin.DECLARED.value
    if detector == "overloaded":
        return Origin.CONDITIONAL.value
    if detector == "enrich":
        return Origin.LLM.value
    if detector == "procmine" or evidence.get("source") == "shipped SQL":
        return Origin.SHIPPED_SQL.value
    return Origin.DATA.value


LABELS = {
    Origin.DECLARED.value: "Declared in the schema",
    Origin.SHIPPED_SQL.value: "Written in your views and procedures",
    Origin.DATA.value: "Inferred from the data",
    Origin.LLM.value: "Guessed by the language model",
    Origin.CONDITIONAL.value: "Holds only for some rows",
}
