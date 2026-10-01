"""Schema linking: which entities and columns does this question need?

Uber's QueryGPT result was that narrowing the search space is the largest single
accuracy lever, and LinkedIn's was to retrieve wide then rank narrow. Both are here:

* **Retrieve wide**, cheaply and deterministically — BM25 over a document per entity
  built from its logical names, synonyms, descriptions and column names, plus fuzzy
  matching for the cryptic cases.
* **Rank narrow** to at most seven entities, pulling in their join neighbours so a
  question that needs a bridge table gets one.
* **Prune columns** so the generator prompt carries only what is relevant, which is
  what keeps a question inside the 6K token budget on a 900-column schema.

An LLM rerank is available but optional: when the margin between the top entities is
comfortable, spending tokens to confirm what BM25 already knows is waste.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from ..semantic.model import SemanticModel

# Words that carry no signal about which table a question is about.
STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "by", "with", "from",
    "and", "or", "is", "are", "was", "were", "be", "been", "how", "what", "which",
    "who", "when", "where", "many", "much", "show", "me", "list", "give", "get",
    "find", "all", "do", "does", "did", "we", "our", "us", "have", "has", "had",
    "there", "their", "it", "its", "that", "this", "these", "those", "per", "each",
    "total", "number", "count", "sum", "average", "top", "most", "least", "best",
}

_WORD = re.compile(r"[a-z0-9]+")

# Plural and inflectional endings, longest first.
_SUFFIXES = ("ies", "es", "s")


def tokenize(text: str) -> list[str]:
    """Content words, with numbers always kept.

    Dropping short tokens discards digits, and a number in a question is never noise:
    with "5" and "9" removed, "top 5 customers" and "top 9 customers" become the same
    string. That made the verified library and the semantic cache answer one with the
    other — returning a confidently wrong result at confidence 0.99.
    """
    words = _WORD.findall(text.lower())
    return [
        stem(w) for w in words
        if w.isdigit() or (w not in STOPWORDS and len(w) > 1)
    ]


def stem(word: str) -> str:
    """Crude singularisation. 'customers' and 'customer' must retrieve the same table."""
    if len(word) <= 3:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "ies":
                return word[: -len(suffix)] + "y"
            if suffix == "es" and word[-3] in "sxzh":
                return word[: -len(suffix)]
            if suffix == "s" and not word.endswith("ss"):
                return word[:-1]
    return word


@dataclass
class EntityDocument:
    entity: str
    terms: Counter = field(default_factory=Counter)
    length: int = 0
    column_terms: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class LinkedEntity:
    entity: str
    score: float
    matched_terms: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class LinkResult:
    entities: list[LinkedEntity] = field(default_factory=list)
    margin: float = 1.0          # how clearly the top set beats the rest
    needs_confirmation: bool = False
    all_scores: dict[str, float] = field(default_factory=dict)
    # Content words from the question that match nothing anywhere in the model. When
    # these are the words carrying the subject, the database simply does not hold what
    # was asked for.
    unmatched_terms: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [e.entity for e in self.entities]

    def to_dict(self) -> dict:
        return {
            "entities": [
                {
                    "entity": e.entity, "score": round(e.score, 3),
                    "matched": e.matched_terms, "columns": e.columns,
                }
                for e in self.entities
            ],
            "margin": round(self.margin, 3),
            "needs_confirmation": self.needs_confirmation,
            "unmatched_terms": self.unmatched_terms,
        }

    @property
    def lexical_overlap(self) -> float:
        """Share of the question's content words that appear anywhere in the model.

        Reported in the trace, and deliberately *not* used to abstain. On a legacy
        schema a perfectly answerable question overlaps with nothing: "total revenue
        across all invoices" matches no term in a model whose entities came out as
        `billing` and `payment`. Low overlap is the problem Stratum exists to solve,
        not evidence that the data is absent.
        """
        total = len(self.matched_terms) + len(self.unmatched_terms)
        return len(self.matched_terms) / total if total else 1.0


class SchemaIndex:
    """A BM25 index over the semantic model, rebuilt whenever the model changes."""

    K1 = 1.4
    B = 0.72

    def __init__(self, model: SemanticModel):
        self.model = model
        self.documents: dict[str, EntityDocument] = {}
        self.document_frequency: Counter = Counter()
        self._build()

    def _build(self) -> None:
        for entity in self.model.accepted_entities():
            terms: Counter = Counter()
            column_terms: dict[str, set[str]] = {}

            def add(text: str, weight: int = 1, _terms: Counter = terms) -> None:
                for token in tokenize(text):
                    _terms[token] += weight

            # The entity's own identity is worth more than any one column.
            add(entity.name.replace("_", " "), 4)
            add(entity.description, 2)
            for synonym in entity.synonyms:
                add(synonym, 3)
            # Its physical table name too: users of a legacy system often ask in its
            # vocabulary, and "how many rows in CUST_MSTR" should still work.
            add(entity.source.replace("_", " ").replace(".", " "), 2)

            for column in entity.columns.values():
                if column.status == "rejected":
                    continue
                tokens = set(tokenize(column.name.replace("_", " ")))
                column_terms[column.name] = tokens
                for token in tokens:
                    terms[token] += 1
                add(column.description, 1)
                for synonym in column.synonyms:
                    add(synonym, 2)
                # Enum labels are how people phrase filters: "cancelled orders".
                for label in column.enum.values():
                    add(str(label), 2)

            document = EntityDocument(
                entity=entity.name, terms=terms,
                length=sum(terms.values()), column_terms=column_terms,
            )
            self.documents[entity.name] = document
            for token in terms:
                self.document_frequency[token] += 1

        self.average_length = (
            sum(d.length for d in self.documents.values()) / len(self.documents)
            if self.documents else 1.0
        )

    def bm25(self, query_terms: list[str], document: EntityDocument) -> tuple[float, list[str]]:
        score = 0.0
        matched: list[str] = []
        total_documents = max(1, len(self.documents))
        for term in query_terms:
            frequency = document.terms.get(term, 0)
            if frequency == 0:
                continue
            matched.append(term)
            df = self.document_frequency.get(term, 0)
            idf = math.log(1 + (total_documents - df + 0.5) / (df + 0.5))
            denominator = frequency + self.K1 * (
                1 - self.B + self.B * document.length / max(1.0, self.average_length)
            )
            score += idf * (frequency * (self.K1 + 1)) / denominator
        return score, matched

    def fuzzy_bonus(self, query_terms: list[str], document: EntityDocument) -> float:
        """Catch near-misses that exact term matching drops.

        A question about "invoices" against an entity the excavator named `billing`
        scores zero on BM25; rapidfuzz recovers some of that without the cost of an
        embedding model.
        """
        from rapidfuzz import fuzz

        if not query_terms or not document.terms:
            return 0.0
        best = 0.0
        vocabulary = list(document.terms)[:120]
        for term in query_terms:
            for token in vocabulary:
                if abs(len(term) - len(token)) > 4:
                    continue
                ratio = fuzz.ratio(term, token) / 100.0
                if ratio > best:
                    best = ratio
        return max(0.0, (best - 0.75) * 4) if best > 0.75 else 0.0

    def search(
        self,
        question: str,
        *,
        limit: int = 7,
        expand_neighbours: bool = True,
    ) -> LinkResult:
        from .joinplan import neighbours

        query_terms = tokenize(question)
        scores: dict[str, tuple[float, list[str]]] = {}
        for name, document in self.documents.items():
            score, matched = self.bm25(query_terms, document)
            score += self.fuzzy_bonus(query_terms, document)
            # A dead or archive table has already been excluded from the model, so
            # everything here is fair game; prefer entities with more rows only as a
            # tiebreak, never as a signal.
            entity = self.model.entity(name)
            if entity is not None and entity.row_estimate:
                score += math.log10(entity.row_estimate + 10) * 0.01
            if score > 0:
                scores[name] = (score, matched)

        ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])
        result = LinkResult(all_scores={k: v[0] for k, v in ranked})

        # Every term the whole model knows about, regardless of which entity won.
        vocabulary: set[str] = set()
        for document in self.documents.values():
            vocabulary |= set(document.terms)
        result.matched_terms = [t for t in query_terms if t in vocabulary]
        result.unmatched_terms = [t for t in query_terms if t not in vocabulary]

        if not ranked:
            return result

        chosen = ranked[:limit]
        # The margin between the last entity kept and the first dropped says how
        # confident the retrieval is. A small margin is what triggers the table chips.
        if len(ranked) > len(chosen):
            top_score = chosen[-1][1][0]
            next_score = ranked[len(chosen)][1][0]
            result.margin = (top_score - next_score) / top_score if top_score else 0.0
        result.needs_confirmation = result.margin < 0.15

        names = [name for name, _ in chosen]
        if expand_neighbours and len(names) > 1:
            # Pull in bridge entities: two selected entities may only connect through
            # a junction table nobody would mention in a question.
            bridges: set[str] = set()
            for name in names:
                for neighbour in neighbours(self.model, name, depth=1):
                    if neighbour in names:
                        continue
                    linked = sum(
                        1 for other in names
                        if neighbour in neighbours(self.model, other, depth=1)
                    )
                    if linked >= 2:
                        bridges.add(neighbour)
            for bridge in sorted(bridges)[:2]:
                chosen.append((bridge, (0.0, [])))

        for name, (score, matched) in chosen:
            entity = self.model.entity(name)
            result.entities.append(
                LinkedEntity(
                    entity=name,
                    score=score,
                    matched_terms=matched,
                    columns=self.relevant_columns(name, query_terms),
                    reason=(
                        "matched " + ", ".join(matched) if matched
                        else "included to connect the other entities"
                    ),
                )
            )
        return result

    def relevant_columns(
        self, entity_name: str, query_terms: list[str], *, keep: int = 14
    ) -> list[str]:
        """Prune an entity's columns to those the question could plausibly want.

        Key columns and anything the question names are always kept; the rest are kept
        only up to a budget, because a 53-column god table would otherwise eat the
        prompt on its own.
        """
        entity = self.model.entity(entity_name)
        document = self.documents.get(entity_name)
        if entity is None or document is None:
            return []

        query = set(query_terms)
        scored: list[tuple[float, str]] = []
        for column in entity.columns.values():
            if column.status == "rejected":
                continue
            tokens = document.column_terms.get(column.name, set())
            overlap = len(tokens & query)
            weight = float(overlap * 3)
            if column.name.lower() in {k.lower() for k in entity.key}:
                weight += 2.0
            if column.enum:
                weight += 0.5
            # Dates and amounts are what questions aggregate and filter on.
            if column.data_type in ("date", "number", "decimal", "integer"):
                weight += 0.4
            scored.append((weight, column.name))

        scored.sort(key=lambda item: (-item[0], item[1]))
        wanted = [name for weight, name in scored if weight > 0][:keep]
        if len(wanted) < keep:
            wanted += [
                name for _, name in scored if name not in wanted
            ][: keep - len(wanted)]
        return wanted
