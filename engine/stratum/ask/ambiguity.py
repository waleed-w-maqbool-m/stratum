"""Ambiguity detection and clarification (PLAN.md 4.4 step 3).

BIRD-Interact and AmbiSQL both make the same point: when a question is genuinely
ambiguous, guessing produces a confident wrong answer, and asking one targeted question
produces a right one. The taxonomy they use is the one used here:

* **intent** — what is being asked is unclear. "Top customers" by what, and how many?
* **implementation** — the intent is clear but a boundary is not. "Recent" over what
  window? Is "last year" the calendar year or the trailing twelve months?
* **knowledge** — a term is used that the model has no definition for. "Active
  customer" means something specific in this business and nothing in general.

Detection is deterministic: a vocabulary of vague terms, plus two checks that only the
semantic model can answer — whether a term is in the glossary, and whether the question
would land on an enum whose labels are still unreviewed guesses.

Crucially, each option is costed. Showing "top 10 by revenue (returns 10 rows)" beside
"every customer above average (returns 47 rows)" makes the choice concrete, and costing
them is a cheap COUNT rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..semantic.model import SemanticModel

# Terms that do not denote anything on their own. Grouped by what is unclear about them.
VAGUE_TERMS: dict[str, tuple[str, str]] = {
    # term: (ambiguity type, what is unresolved)
    "top": ("intent", "how many, and ranked by what"),
    "best": ("intent", "what makes one better than another"),
    "worst": ("intent", "what makes one worse than another"),
    "biggest": ("intent", "measured by which column"),
    "largest": ("intent", "measured by which column"),
    "important": ("intent", "what makes one important"),
    "popular": ("intent", "measured by sales, by plays, or by count"),
    "recent": ("implementation", "over what time window"),
    "lately": ("implementation", "over what time window"),
    "current": ("implementation", "as of when"),
    "last year": ("implementation", "the calendar year, or the trailing twelve months"),
    "last month": ("implementation", "the calendar month, or the trailing thirty days"),
    "this year": ("implementation", "the calendar year to date"),
    "ytd": ("implementation", "from the start of which year"),
    "active": ("knowledge", "what makes a record active in this business"),
    "inactive": ("knowledge", "what makes a record inactive in this business"),
    "churned": ("knowledge", "how long without activity counts as churned"),
    "new": ("knowledge", "how recently created counts as new"),
    "lapsed": ("knowledge", "how long without activity counts as lapsed"),
    "high value": ("knowledge", "what threshold makes a record high value"),
    "at risk": ("knowledge", "what makes a record at risk"),
}

# A question naming a measure explicitly is not ambiguous about its measure, even if it
# says "top".
MEASURE_HINTS = re.compile(
    r"\b(by|per)\s+(revenue|sales|spend|amount|total|value|count|quantity|number|"
    r"orders?|invoices?|price|duration|length)\b",
    re.IGNORECASE,
)

# The same measures named as bare nouns. "The largest invoice total" says what it is
# measuring just as plainly as "ranked by total" does; requiring the preposition meant
# asking the user to repeat a word already in their question.
MEASURE_NOUNS = re.compile(
    r"\b(revenue|sales|spend|spending|amount|totals?|values?|counts?|quantity|"
    r"quantities|price|prices|duration|length|balance|cost)\b",
    re.IGNORECASE,
)

# Superlatives that denote a single row. "The largest X" needs no cut-off — asking
# "how many?" about a question phrased in the singular is noise.
SINGULAR_SUPERLATIVES = frozenset(
    {"largest", "biggest", "smallest", "highest", "lowest", "longest", "shortest"}
)

# An explicit limit removes the "how many" half of a "top" question.
LIMIT_HINT = re.compile(r"\b(top|first|bottom|last)\s+(\d+)\b|\blimit\s+(\d+)\b", re.IGNORECASE)

# An explicit date range removes the "over what window" question.
DATE_HINT = re.compile(
    r"\b(in|during|for|between|since|before|after)\s+"
    r"(\d{4}|january|february|march|april|may|june|july|august|september|october|"
    r"november|december|q[1-4])\b",
    re.IGNORECASE,
)


@dataclass
class ClarificationOption:
    """One concrete reading of the question, with what it would return."""

    id: str
    label: str
    interpretation: str
    logical_sql: str = ""
    row_count: int | None = None
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "interpretation": self.interpretation,
            "row_count": self.row_count,
            "is_default": self.is_default,
        }


@dataclass
class Ambiguity:
    type: str                      # intent | implementation | knowledge
    term: str
    unresolved: str
    options: list[ClarificationOption] = field(default_factory=list)
    question: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "term": self.term,
            "unresolved": self.unresolved,
            "question": self.question,
            "options": [o.to_dict() for o in self.options],
        }


@dataclass
class AmbiguityResult:
    ambiguities: list[Ambiguity] = field(default_factory=list)
    needs_clarification: bool = False
    reason: str = ""

    @property
    def primary(self) -> Ambiguity | None:
        """The one to ask about. Only ever one — a form is not a conversation."""
        if not self.ambiguities:
            return None
        order = {"knowledge": 0, "intent": 1, "implementation": 2}
        return sorted(self.ambiguities, key=lambda a: order.get(a.type, 9))[0]

    def to_dict(self) -> dict[str, Any]:
        primary = self.primary
        return {
            "needs_clarification": self.needs_clarification,
            "reason": self.reason,
            "primary": primary.to_dict() if primary else None,
            "all": [a.to_dict() for a in self.ambiguities],
        }


def detect(
    question: str,
    model: SemanticModel,
    *,
    entities: list[str] | None = None,
) -> AmbiguityResult:
    """Find what is unclear about this question, if anything."""
    result = AmbiguityResult()
    lowered = question.lower()

    # A measure counts as given whether the question says "by revenue" or just
    # "revenue" — the user named it either way.
    has_measure = bool(MEASURE_HINTS.search(question) or MEASURE_NOUNS.search(question))
    has_limit = bool(LIMIT_HINT.search(question))
    has_date = bool(DATE_HINT.search(question))

    for term, (kind, unresolved) in VAGUE_TERMS.items():
        if not re.search(rf"\b{re.escape(term)}\b", lowered):
            continue

        # A term in the glossary is not ambiguous; that is what the glossary is for.
        if _in_glossary(term, model):
            continue

        # "New customers" needs a business definition; "old and new values" and "New
        # York" do not. A knowledge term is only unclear when it qualifies something
        # the database models — sending "every audit entry with the old and new
        # values" back for clarification stopped the cost guard from ever seeing it.
        if kind == "knowledge" and not _qualifies_entity(term, lowered, model):
            continue

        # The question may already have answered the thing the term leaves open.
        # A singular superlative supplies its own cut-off: "the largest" is one row.
        settled_count = has_limit or term in SINGULAR_SUPERLATIVES
        if kind == "intent" and has_measure and settled_count:
            continue
        if kind == "implementation" and has_date:
            continue

        result.ambiguities.append(
            Ambiguity(
                type=kind,
                term=term,
                unresolved=unresolved,
                question=_phrase_question(term, kind, unresolved),
            )
        )

    # An enum whose labels are still guesses is a knowledge ambiguity of its own: the
    # question may be filtering on a label nobody has confirmed.
    for entity_name in entities or []:
        entity = model.entity(entity_name)
        if entity is None:
            continue
        for column in entity.columns.values():
            if not column.enum or column.status != "hypothesis":
                continue
            hit = next(
                (
                    label for label in column.enum.values()
                    if label and re.search(rf"\b{re.escape(str(label).lower())}\b", lowered)
                ),
                None,
            )
            if hit:
                result.ambiguities.append(
                    Ambiguity(
                        type="knowledge",
                        term=str(hit),
                        unresolved=(
                            f"{entity.name}.{column.name} was decoded by inference, and "
                            f"nobody has confirmed that its codes mean what they appear to"
                        ),
                        question=(
                            f"I read \"{hit}\" as {entity.name}.{column.name}, but that "
                            "mapping is an unreviewed guess. Is it right?"
                        ),
                    )
                )

    result.needs_clarification = bool(result.ambiguities)
    if result.needs_clarification:
        primary = result.primary
        result.reason = f"{primary.type} ambiguity: '{primary.term}' — {primary.unresolved}"
    return result


def _in_glossary(term: str, model: SemanticModel) -> bool:
    lowered = term.lower()
    return any(key.lower() == lowered for key in model.glossary)


def _entity_nouns(model: SemanticModel) -> set[str]:
    """Every word a question might use to name an entity, singular."""
    nouns: set[str] = set()
    for entity in model.entities.values():
        for name in (entity.name, *entity.synonyms):
            for word in re.split(r"[\s_]+", name.lower()):
                if len(word) > 2:
                    nouns.add(word)
    return nouns


def _qualifies_entity(term: str, question: str, model: SemanticModel) -> bool:
    """Is `term` followed by a word that names something in the model?"""
    nouns = _entity_nouns(model)
    for match in re.finditer(rf"\b{re.escape(term)}\s+([a-z]+)", question):
        word = match.group(1)
        for candidate in (word, word.rstrip("s"), word[:-2] if word.endswith("es") else word):
            if candidate in nouns:
                return True
    return False


def _phrase_question(term: str, kind: str, unresolved: str) -> str:
    if kind == "intent":
        return f'When you say "{term}", {unresolved}?'
    if kind == "implementation":
        return f'"{term.title()}" — {unresolved}?'
    return f'What counts as "{term}" here? I do not have a definition for it.'


# --------------------------------------------------------------------- options


def _ranked_subject(term: str, question: str, model: SemanticModel, entities: list[str]) -> str:
    """The entity a ranking word qualifies: "best customers" ranks customers."""
    for match in re.finditer(rf"\b{re.escape(term)}\s+([a-z]+)", question.lower()):
        word = match.group(1)
        forms = {word, word.rstrip("s"), word[:-2] if word.endswith("es") else word}
        for entity in model.entities.values():
            names = {entity.name.lower(), *(s.lower() for s in entity.synonyms)}
            if names & forms:
                return entity.name
    return entities[0] if entities else ""


def _ranking_scope(model: SemanticModel, subject: str, entities: list[str]) -> list[str]:
    """The subject and the tables that point at it: where a ranking of the subject can
    get its numbers from. A film's release year says nothing about a customer."""
    children = [
        rel.from_entity for rel in model.relationships
        if rel.to_entity == subject and rel.from_entity != subject
    ]
    # Prefer children the question already linked to, then the rest.
    children.sort(key=lambda name: (name not in entities, name))
    return list(dict.fromkeys([*children, subject]))


def build_options(
    ambiguity: Ambiguity,
    model: SemanticModel,
    entities: list[str],
    question: str = "",
) -> list[ClarificationOption]:
    """Two to four concrete readings, phrased as things the user can pick.

    The options are built from the model, not invented: "by total revenue" is only
    offered when an entity in scope actually has an amount column.
    """
    options: list[ClarificationOption] = []
    measures = _available_measures(model, entities)

    if ambiguity.type == "intent" and ambiguity.term in (
        "top", "best", "biggest", "largest", "popular", "worst", "important"
    ):
        subject = _ranked_subject(ambiguity.term, question, model, entities)
        scope = _ranking_scope(model, subject, entities) if subject else entities
        measures = _available_measures(model, scope)
        for index, (label, _expression) in enumerate(measures[:3]):
            options.append(
                ClarificationOption(
                    id=f"measure-{index}",
                    label=f"Top 10 by {label}",
                    interpretation=f"rank by {label}, highest first, limit 10",
                    is_default=index == 0,
                )
            )
        # "Best" is as often "most often" as "most money", and a question with one
        # answer is not a question; the count reading is always on the list.
        children = [name for name in scope if name != subject]
        counted = children[0].replace("_", " ") if children else "related"
        options.append(
            ClarificationOption(
                id="measure-count",
                label=f"Top 10 by number of {counted} records",
                interpretation=f"rank by how many {counted} records each has, highest first, limit 10",
                is_default=not options,
            )
        )

    elif ambiguity.type == "implementation":
        span = _date_span(model, entities)
        latest = span[1] if span else None
        year = latest[:4] if latest else ""
        options = [
            ClarificationOption(
                id="calendar-year",
                label=f"The calendar year {year}" if year else "The most recent full year",
                interpretation=(
                    f"records dated within {year}" if year
                    else "records dated within the latest year present in the data"
                ),
                is_default=True,
            ),
            ClarificationOption(
                id="trailing-12",
                label="The trailing twelve months",
                interpretation=f"the twelve months ending {latest or 'at the latest record'}",
            ),
            ClarificationOption(
                id="all-time",
                label="All of it",
                interpretation="no date bound at all",
            ),
        ]

    elif ambiguity.type == "knowledge":
        options = [
            ClarificationOption(
                id="has-activity",
                label="Has at least one related record",
                interpretation=f"{ambiguity.term} means it appears in a child table",
                is_default=True,
            ),
            ClarificationOption(
                id="recent-activity",
                label="Has activity in the last twelve months",
                interpretation=f"{ambiguity.term} means recent activity, not any activity",
            ),
            ClarificationOption(
                id="not-soft-deleted",
                label="Is not marked deleted",
                interpretation=(
                    f"{ambiguity.term} refers only to the soft-delete flag, which is "
                    "already applied by default"
                ),
            ),
        ]

    return options


def _available_measures(model: SemanticModel, entities: list[str]) -> list[tuple[str, str]]:
    """Numeric columns in scope that a ranking could plausibly use."""

    preferred = ("amount", "total", "revenue", "price", "value", "quantity", "spend")
    # Numbers that name something rather than measure it. Summing them is meaningless,
    # and offering "top 10 by total id" as a reading of "best customers" is worse than
    # offering nothing.
    # Statuses and types are numbers too, and summing them is just as meaningless.
    identifier_words = {
        "id", "number", "no", "nbr", "code", "key", "reference", "ref",
        "status", "type", "flag", "kind", "year",
    }
    joined = {
        (rel.from_entity.lower(), column.lower())
        for rel in model.relationships for column in rel.from_columns
    } | {
        (rel.to_entity.lower(), column.lower())
        for rel in model.relationships for column in rel.to_columns
    }
    out: list[tuple[int, str, str]] = []
    for name in entities:
        entity = model.entity(name)
        if entity is None:
            continue
        # The key is stored by physical name; compare against the column's physical
        # source too, or a logical `id` over physical `cstmr_id` slips through.
        keys = {k.lower() for k in entity.key}
        for column in entity.columns.values():
            if column.data_type not in ("number", "decimal", "integer"):
                continue
            physical = (column.physical_column or "").lower()
            if column.name.lower() in keys or physical in keys:
                continue
            if (entity.name.lower(), physical) in joined:
                continue
            # Raw words, not `tokenize`: it drops "number" and "total" as stopwords.
            words = set(re.findall(r"[a-z]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", column.name).lower()))
            if words & identifier_words:
                continue
            rank = next(
                (i for i, hint in enumerate(preferred) if hint in words), len(preferred)
            )
            # "total total amount" reads as a stutter; the column already says total.
            readable = column.name.replace("_", " ")
            label = readable if "total" in words else f"total {readable}"
            out.append((rank, label, f"SUM({entity.name}.{column.name})"))

    out.sort(key=lambda item: item[0])
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for _, label, expression in out:
        if label in seen:
            continue
        seen.add(label)
        unique.append((label, expression))
    return unique


def _date_span(model: SemanticModel, entities: list[str]) -> tuple[str, str] | None:
    """The date range the model knows about, used to phrase time options concretely."""
    for name in entities:
        entity = model.entity(name)
        if entity is None:
            continue
        for column in entity.columns.values():
            if column.data_type == "date":
                # The model does not store min/max; the caller can fill this in with a
                # probe when it matters. Returning None keeps the phrasing generic
                # rather than inventing a range.
                return None
    return None


def cost_options(
    options: list[ClarificationOption], executor, model, compile_fn
) -> list[ClarificationOption]:
    """Attach a row count to each option, so the choice is concrete.

    PLAN.md 4.4 step 3: each option shows how many rows it would return. An option with
    no SQL attached is left uncosted rather than guessed at.
    """
    for option in options:
        if not option.logical_sql:
            continue
        try:
            compiled = compile_fn(option.logical_sql, model, executor.profile)
            probe = f"SELECT COUNT(*) FROM ({compiled.physical_sql}) AS _opt"
            result = executor.run(probe, row_cap=2)
            option.row_count = result.rows[0][0] if result.rows else None
        except Exception:
            option.row_count = None
    return options
