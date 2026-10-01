"""The semantic cache (PLAN.md 4.4 step 1).

A question that has been answered before should not be answered again. The cache sits
beside the verified library on the same rung of the ladder (R0), but it means something
weaker and is treated accordingly:

* a **verified** entry was confirmed by a person, is trusted indefinitely, and returns
  confidence 0.99;
* a **cached** entry was merely produced by the system and agreed with itself, is
  trusted for a bounded time, and returns whatever confidence it originally earned —
  minus a little, because the data may have moved underneath it.

That distinction is the point. A cache that presents stale machine output with the
authority of a human decision is how a system loses trust in one afternoon.

Entries are invalidated when the semantic model version changes, because a model change
is precisely the thing that would alter the answer.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# How long a cached answer is reused for. Short by default: the underlying data is a
# live database, and a day-old count is a wrong count.
DEFAULT_TTL_SECONDS = 3600

# Questions have to be this similar to count as the same question.
MATCH_THRESHOLD = 0.9


@dataclass
class CacheEntry:
    question: str
    logical_sql: str
    model_version: int
    entities: list[str] = field(default_factory=list)
    confidence: float = 0.0
    created_at: float = field(default_factory=time.time)
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_fresh(self, ttl: int, model_version: int) -> bool:
        if self.model_version != model_version:
            return False        # the mapping changed; the answer may have too
        return (time.time() - self.created_at) < ttl


class SemanticCache:
    """Question → logical SQL, scoped to one database and one model version.

    Only the *SQL* is cached, never the rows. Re-running a cheap query against live data
    is right; serving yesterday's rows as today's answer is not.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl: int = DEFAULT_TTL_SECONDS,
        max_entries: int = 500,
    ):
        self.path = path
        self.ttl = ttl
        self.max_entries = max_entries
        self.entries: list[CacheEntry] = []
        if path is not None and path.exists():
            self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (ValueError, OSError):
            return
        self.entries = [CacheEntry(**item) for item in raw.get("entries", [])]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"entries": [e.to_dict() for e in self.entries]}, indent=2),
            "utf-8",
        )

    def lookup(self, question: str, model_version: int) -> CacheEntry | None:
        from ..semantic.verified import similarity

        best: tuple[float, CacheEntry] | None = None
        for entry in self.entries:
            if not entry.is_fresh(self.ttl, model_version):
                continue
            score = similarity(question, entry.question)
            if best is None or score > best[0]:
                best = (score, entry)
        if best is not None and best[0] >= MATCH_THRESHOLD:
            best[1].hits += 1
            self.save()
            return best[1]
        return None

    def put(
        self,
        question: str,
        logical_sql: str,
        model_version: int,
        *,
        entities: list[str] | None = None,
        confidence: float = 0.0,
    ) -> None:
        """Remember an answer. Only worth doing for answers worth reusing."""
        if not logical_sql.strip():
            return
        # A low-confidence answer is not worth repeating faster.
        if confidence < 0.6:
            return

        self.entries = [
            e for e in self.entries
            if e.question.strip().lower() != question.strip().lower()
        ]
        self.entries.append(
            CacheEntry(
                question=question,
                logical_sql=logical_sql,
                model_version=model_version,
                entities=entities or [],
                confidence=confidence,
            )
        )
        # Evict oldest first; the cache is a convenience, not a store.
        if len(self.entries) > self.max_entries:
            self.entries.sort(key=lambda e: e.created_at)
            self.entries = self.entries[-self.max_entries:]
        self.save()

    def invalidate(self, model_version: int | None = None) -> int:
        """Drop everything, or everything from a superseded model version."""
        before = len(self.entries)
        if model_version is None:
            self.entries = []
        else:
            self.entries = [e for e in self.entries if e.model_version == model_version]
        self.save()
        return before - len(self.entries)

    def stats(self) -> dict[str, Any]:
        fresh = sum(
            1 for e in self.entries
            if (time.time() - e.created_at) < self.ttl
        )
        return {
            "entries": len(self.entries),
            "fresh": fresh,
            "hits": sum(e.hits for e in self.entries),
        }

    def __len__(self) -> int:
        return len(self.entries)
