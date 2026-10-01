"""Plain-English explanation of what happened (PLAN.md 4.4 step 14).

Two things get explained, and the second matters more:

* what the query did, which is mostly a readback of the SQL, and
* **every assumption the compiler applied** — "excluded 412 soft-deleted customers",
  "converted amounts stored in cents after 2023-01-01".

The second is the difference between a tool people trust and one they check by hand.
A count that came back lower than expected is not a bug if the user can see that 412
rows were excluded and why; it is a mystery otherwise.

No LLM is used here. The assumptions are already prose, written by whichever detector
found the rule, and spending tokens to reword them would add nothing but latency.
"""

from __future__ import annotations

from ..semantic.model import SemanticModel


def explain_answer(answer, model: SemanticModel) -> str:
    parts: list[str] = []

    # --- what was asked of the database
    entities = answer.entities or []
    if entities:
        readable = _join_words([e.replace("_", " ") for e in entities])
        parts.append(f"Read {readable}.")

    if answer.join_plan and answer.join_plan.steps:
        joins = ", ".join(
            f"{s.from_entity.replace('_', ' ')} to {s.to_entity.replace('_', ' ')}"
            for s in answer.join_plan.steps
        )
        inferred = [s for s in answer.join_plan.steps if s.confidence < 0.95]
        sentence = f"Joined {joins}."
        if inferred:
            sentence += (
                " Those joins are not declared in the database; they were inferred "
                "from the data."
            )
        parts.append(sentence)

    # --- the assumptions, which is the part worth reading
    if answer.assumptions:
        parts.append("")
        parts.append("Assumptions applied:")
        for assumption in answer.assumptions:
            line = f"  • {assumption['text']}"
            if assumption.get("rows_excluded"):
                line += f" ({assumption['rows_excluded']:,} rows excluded)"
            parts.append(line)

    # --- anything the model is unsure about that this answer depends on
    unreviewed = _unreviewed(answer, model)
    if unreviewed:
        parts.append("")
        parts.append(
            "Unreviewed mappings this answer depends on — confirm these in the "
            "Workbench:"
        )
        for item in unreviewed[:5]:
            parts.append(f"  • {item}")

    # --- result shape
    if answer.row_count:
        noun = "row" if answer.row_count == 1 else "rows"
        line = f"Returned {answer.row_count:,} {noun}."
        if answer.truncated:
            line += " The result was truncated at the row cap."
        parts.append("")
        parts.append(line)
    elif answer.status == "ok":
        parts.append("")
        parts.append(
            "The query returned no rows. That may be the right answer, or it may mean "
            "one of the filters above is excluding more than it should."
        )

    if answer.repairs:
        parts.append(
            f"The query was corrected {answer.repairs} time(s) before it ran."
        )

    return "\n".join(parts).strip()


def _unreviewed(answer, model: SemanticModel) -> list[str]:
    """Hypothesis-status mappings the answer leans on."""
    out: list[str] = []
    for entity_name in answer.entities or []:
        entity = model.entity(entity_name)
        if entity is None:
            continue
        for filt in entity.active_filters():
            if filt.status == "hypothesis":
                out.append(
                    f"{entity_name}: {filt.reason} — this filter has not been confirmed"
                )
        for column in entity.columns.values():
            if column.status == "hypothesis" and column.is_derived and column.note:
                out.append(f"{entity_name}.{column.name}: {column.note}")
    return out


def _join_words(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def describe_sql(logical_sql: str) -> str:
    """A one-line readback of what a query computes, for the trace panel."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(logical_sql)
    except Exception:
        return ""
    if not isinstance(tree, exp.Select):
        return ""

    measures = []
    for node in tree.find_all(exp.AggFunc):
        name = node.sql_name().lower()
        target = node.this.sql() if node.this else ""
        measures.append(f"{name} of {target}" if target != "*" else f"{name} of rows")
    grouping = [g.sql() for g in tree.args.get("group") or []]

    if measures and grouping:
        return f"{_join_words(measures)}, grouped by {_join_words(grouping)}"
    if measures:
        return _join_words(measures)
    return "a list of matching records"
