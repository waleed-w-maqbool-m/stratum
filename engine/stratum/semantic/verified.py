"""The verified query library (PLAN.md 4.3, 4.4 step 1).

Snowflake's Verified Query Repository and Databricks' trusted assets are the same idea:
a query a person has confirmed is correct for a question, reused whenever that question
comes back. LinkedIn found example queries were the single biggest accuracy lever they
had.

Two properties matter here:

* Entries are stored as **logical** SQL, so they survive physical changes. Rename a
  column in the legacy database, fix the mapping once in the semantic model, and every
  verified query keeps working.
* A hit costs **zero generation tokens** and returns confidence 0.99, which on a free
  tier is the difference between answering instantly and queueing behind a rate limit.

Matching is lexical here (token overlap plus rapidfuzz). PLAN.md 4.4 specifies
`fastembed` embeddings; that is an optional extra, and `EmbeddingMatcher` is used when
it is installed. The lexical path is the fallback and is what the tests run against,
since it needs no model download.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Above this, the stored question is treated as the same question.
MATCH_THRESHOLD = 0.86

# Below this a verified example is not even worth showing the generator.
EXAMPLE_THRESHOLD = 0.25


@dataclass
class VerifiedQuery:
    question: str
    logical_sql: str
    params: dict[str, Any] = field(default_factory=dict)
    verified_by: str = ""
    verified_at: float = field(default_factory=time.time)
    entities: list[str] = field(default_factory=list)
    uses: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tokens(text: str) -> set[str]:
    """Delegates to the linker's tokeniser rather than reimplementing it.

    It had its own copy, which is how a fix to one missed the other: numbers were
    dropped here long after they were kept there, so "top 5 customers" still matched
    "top 9 customers" and the library answered one with the other.
    """
    from ..ask.linking import tokenize

    return set(tokenize(text))


def similarity(left: str, right: str) -> float:
    """How likely two questions are the same question.

    Jaccard over content words, nudged by a whole-string fuzzy ratio so that word
    order and small rephrasings do not dominate.
    """
    from rapidfuzz import fuzz

    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    ratio = fuzz.token_sort_ratio(left.lower(), right.lower()) / 100.0
    return round(0.65 * jaccard + 0.35 * ratio, 4)


class VerifiedLibrary:
    """Verified queries for one database, persisted as JSON."""

    def __init__(self, path: Path | None = None, entries: list[VerifiedQuery] | None = None):
        self.path = path
        self.entries: list[VerifiedQuery] = entries or []
        if path is not None and path.exists():
            self.load()

    # ------------------------------------------------------------------ storage
    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        raw = json.loads(self.path.read_text("utf-8"))
        self.entries = [VerifiedQuery(**item) for item in raw.get("queries", [])]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"queries": [e.to_dict() for e in self.entries]}, indent=2
            ),
            "utf-8",
        )

    def add(self, entry: VerifiedQuery) -> VerifiedQuery:
        existing = next(
            (e for e in self.entries if e.question.strip().lower()
             == entry.question.strip().lower()),
            None,
        )
        if existing is not None:
            existing.logical_sql = entry.logical_sql
            existing.verified_by = entry.verified_by
            existing.verified_at = entry.verified_at
            existing.entities = entry.entities
            self.save()
            return existing
        self.entries.append(entry)
        self.save()
        return entry

    def remove(self, question: str) -> bool:
        before = len(self.entries)
        self.entries = [
            e for e in self.entries
            if e.question.strip().lower() != question.strip().lower()
        ]
        if len(self.entries) != before:
            self.save()
            return True
        return False

    # ------------------------------------------------------------------ matching
    def match(self, question: str, threshold: float = MATCH_THRESHOLD) -> VerifiedQuery | None:
        """The stored query for this question, if there is one confident enough."""
        best: tuple[float, VerifiedQuery] | None = None
        for entry in self.entries:
            score = similarity(question, entry.question)
            if best is None or score > best[0]:
                best = (score, entry)
        if best is not None and best[0] >= threshold:
            best[1].uses += 1
            self.save()
            return best[1]
        return None

    def examples_for(
        self, entities: list[str], limit: int = 3
    ) -> list[tuple[str, str]]:
        """Verified queries over the same entities, for the generator prompt.

        Retrieved by entity overlap rather than by question similarity: a verified
        query about the same tables teaches the model how *this* schema is joined and
        named, which is the part it cannot guess.
        """
        wanted = {e.lower() for e in entities}
        if not wanted:
            return []
        scored: list[tuple[float, VerifiedQuery]] = []
        for entry in self.entries:
            if not entry.entities:
                continue
            overlap = len(wanted & {e.lower() for e in entry.entities})
            if not overlap:
                continue
            scored.append((overlap / len(wanted | {e.lower() for e in entry.entities}), entry))
        scored.sort(key=lambda item: -item[0])
        return [
            (entry.question, entry.logical_sql)
            for score, entry in scored[:limit]
            if score >= EXAMPLE_THRESHOLD
        ]

    def suggest_from_feedback(
        self, question: str, logical_sql: str, entities: list[str]
    ) -> VerifiedQuery:
        """A candidate for the library, pending a human's confirmation.

        PLAN.md 4.4 step 15: a "looks right" on a high-value question proposes an entry;
        a person confirms it. Never added silently — the whole value of the library is
        that someone stood behind each entry.
        """
        return VerifiedQuery(
            question=question,
            logical_sql=logical_sql,
            entities=entities,
            verified_by="",
            note="proposed from positive feedback; not yet confirmed",
        )

    def __len__(self) -> int:
        return len(self.entries)
