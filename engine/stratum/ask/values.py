"""Value linking: mapping what a person typed onto what is actually stored.

A question says "customers in New York". The column holds `'NY '`, `'N.Y.'` and
`'New York'` — three spellings of one place, one of them space-padded because the column
is CHAR(10). Generating `WHERE city = 'New York'` returns a third of the right answer
and nobody notices, because a third of the right answer looks like an answer.

So literals in a question are resolved against a **value index** before generation: an
inverted trigram index over the low-cardinality string columns, with rapidfuzz for the
near misses. The index is built from the profiles already collected during excavation,
so it costs no extra queries, and it only covers columns where enumerating values is
meaningful — a column with 40,000 distinct addresses is not a vocabulary.

The output is a set of candidate bindings the compiler can turn into an `IN` list, which
is what makes "New York" match all three spellings at once.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..semantic.model import SemanticModel

# A column with more distinct values than this is data, not a vocabulary.
MAX_VOCABULARY = 400

# Below this a fuzzy match is a coincidence.
MIN_SCORE = 78

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&.'\- ]{1,40}")


def trigrams(text: str) -> set[str]:
    folded = f"  {re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()}  "
    return {folded[i : i + 3] for i in range(len(folded) - 2)}


@dataclass
class ValueBinding:
    """One literal from the question, and the stored values it should match."""

    literal: str
    entity: str
    column: str
    matches: list[str] = field(default_factory=list)
    score: float = 0.0
    exact: bool = False

    def predicate(self) -> str:
        """The logical predicate that captures every spelling at once."""
        if len(self.matches) == 1:
            return f"{self.entity}.{self.column} = {_quote(self.matches[0])}"
        values = ", ".join(_quote(v) for v in self.matches)
        return f"{self.entity}.{self.column} IN ({values})"

    def to_dict(self) -> dict:
        return {
            "literal": self.literal,
            "entity": self.entity,
            "column": self.column,
            "matches": self.matches,
            "score": round(self.score, 3),
            "exact": self.exact,
            "predicate": self.predicate(),
        }


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class ValueIndex:
    """Trigram + fuzzy index over the vocabulary columns of a semantic model.

    Built from `ProfileSet.top_values`, which excavation already collected. Only string
    columns with a small distinct count are indexed; everything else is skipped, because
    matching a question literal against a free-text column produces noise, not bindings.
    """

    def __init__(self, model: SemanticModel, profiles=None):
        self.model = model
        # trigram -> {(entity, column, stored_value)}
        self.index: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        self.vocabulary: dict[tuple[str, str], list[str]] = {}
        self._build(profiles)

    def _build(self, profiles) -> None:
        for entity in self.model.accepted_entities():
            for column in entity.columns.values():
                if column.status == "rejected":
                    continue

                values = self._values_for(entity, column, profiles)
                if not values:
                    continue
                self.vocabulary[(entity.name, column.name)] = values
                for value in values:
                    for gram in trigrams(value):
                        self.index[gram].add((entity.name, column.name, value))

    def _values_for(self, entity, column, profiles) -> list[str]:
        """The vocabulary of one column, if it has one."""
        # An enum's labels are the best vocabulary there is: they are what a person
        # would say, and they map to codes nobody would say.
        if column.enum:
            return sorted({str(v) for v in column.enum.values() if str(v).strip()})

        # Stored with the model, so this works without a live ProfileSet.
        stored = self.model.vocabulary.get(f"{entity.name}.{column.name}")
        if stored:
            return stored

        if profiles is None:
            return []
        profile = profiles.get(entity.source, column.physical_column or column.name)
        if profile is None or profile.numeric:
            return []
        if not 0 < profile.distinct_count <= MAX_VOCABULARY:
            return []
        shape = profile.value_shape
        if shape is not None and shape.avg_length > 48:
            return []           # free text, not a vocabulary

        return sorted({
            str(v).strip() for v, _ in profile.top_values
            if v is not None and str(v).strip()
        })

    # ------------------------------------------------------------------ lookup
    def lookup(
        self, literal: str, *, entities: list[str] | None = None, limit: int = 4
    ) -> list[ValueBinding]:
        """Where in the schema does this literal live, and as what?"""
        from rapidfuzz import fuzz

        wanted = {e.lower() for e in entities} if entities else None
        grams = trigrams(literal)
        if not grams:
            return []

        # Candidate set from the trigram index, then scored properly.
        hits: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
        seen: set[tuple[str, str, str]] = set()
        for gram in grams:
            for candidate in self.index.get(gram, ()):
                if candidate in seen:
                    continue
                seen.add(candidate)
                entity, column, stored = candidate
                if wanted is not None and entity.lower() not in wanted:
                    continue
                # `partial_ratio` scores a substring at 100, so 'USA' against the
                # audit code 'U' comes back a perfect match. Only allow the partial
                # score when the stored value is substantial enough for containment to
                # mean something.
                comparable = min(len(literal), len(stored)) >= max(
                    3, 0.6 * max(len(literal), len(stored))
                )
                score = fuzz.ratio(literal.lower(), stored.lower())
                if comparable:
                    score = max(
                        score, fuzz.partial_ratio(literal.lower(), stored.lower())
                    )
                if score >= MIN_SCORE:
                    hits[(entity, column)].append((score, stored))

        bindings: list[ValueBinding] = []
        for (entity, column), scored in hits.items():
            scored.sort(key=lambda item: -item[0])
            best = scored[0][0]
            # Keep every spelling that scores nearly as well as the best one: that is
            # the whole point — 'NY', 'N.Y.' and 'New York' are one answer, not three.
            matches = [value for score, value in scored if score >= max(MIN_SCORE, best - 8)]
            bindings.append(
                ValueBinding(
                    literal=literal,
                    entity=entity,
                    column=column,
                    matches=matches[:8],
                    score=best / 100.0,
                    exact=any(m.lower() == literal.lower() for m in matches),
                )
            )

        bindings.sort(key=lambda b: (-b.score, len(b.matches)))
        return bindings[:limit]

    def extract_literals(self, question: str) -> list[str]:
        """Quoted strings and capitalised phrases — the things worth looking up.

        Deliberately conservative: looking up every word would bind 'many' to a city
        called Many and produce a filter nobody asked for.
        """
        out: list[str] = []
        for match in re.finditer(r"['\"]([^'\"]{2,40})['\"]", question):
            out.append(match.group(1).strip())

        # Capitalised runs that are not the first word of the sentence.
        for match in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*)*)", question):
            phrase = match.group(1).strip()
            if len(phrase) > 1 and phrase.lower() not in _COMMON_CAPITALS:
                out.append(phrase)

        seen: set[str] = set()
        unique: list[str] = []
        for literal in out:
            key = literal.lower()
            if key not in seen:
                seen.add(key)
                unique.append(literal)
        return unique

    def bind(
        self, question: str, entities: list[str] | None = None
    ) -> list[ValueBinding]:
        """Every literal in the question that resolves to stored values."""
        out: list[ValueBinding] = []
        for literal in self.extract_literals(question):
            matches = self.lookup(literal, entities=entities, limit=1)
            out.extend(m for m in matches if m.matches)
        return out

    def stats(self) -> dict:
        return {
            "columns_indexed": len(self.vocabulary),
            "values_indexed": sum(len(v) for v in self.vocabulary.values()),
            "trigrams": len(self.index),
        }


_COMMON_CAPITALS = {
    "i", "sql", "id", "how", "what", "which", "who", "when", "where", "show",
    "list", "give", "find", "count", "total", "the", "a", "an",
}


def render_bindings(bindings: list[ValueBinding]) -> str:
    """The lines the generator prompt carries, so the model uses stored spellings.

    Given to the model as a fact rather than a suggestion: it cannot know that the
    database spells the United States four different ways, and asking it to guess is how
    a filter silently drops two thirds of the rows.
    """
    if not bindings:
        return ""
    lines = ["VALUES (use these exactly — the database spells them this way)"]
    for binding in bindings:
        spellings = ", ".join(_quote(m) for m in binding.matches)
        lines.append(f'  "{binding.literal}" is {binding.entity}.{binding.column} in ({spellings})')
    return "\n".join(lines)
