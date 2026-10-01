"""Cost guard.

Before a query runs, ask the server what it would cost. If the plan says a large
table will be scanned, stop and hand the decision to a human along with a cheaper
variant, rather than discovering the problem by waiting three minutes for it.

PLAN.md 4.4 step 8. The anti-pattern this exists for is #16: a big append-only table
with no index on the column everyone filters by.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from .base import Connector, CostEstimate


@dataclass
class CostVerdict:
    allowed: bool
    reason: str = ""
    estimate: CostEstimate | None = None
    scanned_tables: list[str] = field(default_factory=list)
    cheaper_variant: str | None = None
    cheaper_explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        est = self.estimate
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "est_rows": round(est.estimated_rows) if est else None,
            "est_cost": round(est.estimated_cost, 2) if est else None,
            "est_ms": round(est.estimated_ms) if est and est.estimated_ms else None,
            "scanned_tables": self.scanned_tables,
            "cheaper_variant": self.cheaper_variant,
            "cheaper_explanation": self.cheaper_explanation,
        }


@dataclass
class CostPolicy:
    """Where the line is. Tuned per connection, not per query."""

    max_scanned_rows: int = 2_000_000
    max_estimated_cost: float = 50_000.0
    max_estimated_ms: float = 20_000.0
    # A scan of a table this big is worth stopping for even if the plan looks cheap.
    big_table_rows: int = 500_000


# However small a database, a scan below this many rows is never worth stopping.
MIN_BIG_TABLE_ROWS = 100_000


class CostGuard:
    def __init__(self, connector: Connector, policy: CostPolicy | None = None):
        self.connector = connector
        self.policy = policy or CostPolicy()
        # An explicit policy is taken as written; the default one is tuned to this
        # connection the first time it is needed (see `big_table_rows`).
        self._explicit = policy is not None
        self._row_counts: dict[str, int] | None = None

    def big_table_rows(self) -> int:
        """How many rows make a table big *in this database*.

        The fixed default is sized for an 8-million-row audit log. Against a database
        whose working tables hold sixteen thousand rows, a 400,000-row log is the one
        table nobody should scan — and a fixed half-million threshold never noticed it.
        Twenty times the size of a typical large table (the 90th percentile), clamped
        between a floor and the configured ceiling.
        """
        if self._explicit:
            return self.policy.big_table_rows
        sizes = sorted(n for n in self.row_counts().values() if n > 0)
        if not sizes:
            return self.policy.big_table_rows
        typical = sizes[int(0.9 * (len(sizes) - 1))]
        return max(MIN_BIG_TABLE_ROWS, min(self.policy.big_table_rows, 20 * typical))

    def row_counts(self) -> dict[str, int]:
        if self._row_counts is None:
            self._row_counts = {
                t.name.lower(): t.row_estimate for t in self.connector.list_tables()
            }
        return self._row_counts

    def check(self, sql: str) -> CostVerdict:
        estimate = self.connector.explain(sql)
        if not estimate.supported:
            # Without a plan we fall back to "is a big table in the FROM clause with
            # no bound on it", which is coarse but better than nothing.
            return self._static_check(sql, estimate)

        counts = self.row_counts()
        threshold = self.big_table_rows()
        big_scans = [
            name for name in estimate.scans
            if counts.get(name.lower(), 0) >= threshold
        ]
        scanned_rows = sum(counts.get(name.lower(), 0) for name in estimate.scans)

        breaches = []
        if scanned_rows > self.policy.max_scanned_rows:
            breaches.append(f"the plan scans about {scanned_rows:,} rows")
        if estimate.estimated_cost > self.policy.max_estimated_cost:
            breaches.append(f"estimated cost {estimate.estimated_cost:,.0f}")
        if estimate.estimated_ms and estimate.estimated_ms > self.policy.max_estimated_ms:
            breaches.append(f"estimated {estimate.estimated_ms / 1000:,.0f} s")
        if big_scans:
            breaches.append(
                "full scan of " + ", ".join(f"{t} ({counts.get(t.lower(), 0):,} rows)"
                                            for t in big_scans)
            )

        if not breaches:
            return CostVerdict(True, estimate=estimate, scanned_tables=estimate.scans)

        variant, explanation = self.cheaper_variant(sql, big_scans or estimate.scans)
        return CostVerdict(
            allowed=False,
            reason="; ".join(breaches),
            estimate=estimate,
            scanned_tables=estimate.scans,
            cheaper_variant=variant,
            cheaper_explanation=explanation,
        )

    def _static_check(self, sql: str, estimate: CostEstimate) -> CostVerdict:
        counts = self.row_counts()
        try:
            tree = sqlglot.parse_one(sql, read=self.connector.profile.sqlglot_dialect)
        except Exception:
            return CostVerdict(True, "plan unavailable and SQL unparseable", estimate)

        referenced = [
            t.name.lower() for t in tree.find_all(exp.Table) if t.name
        ]
        threshold = self.big_table_rows()
        big = [t for t in referenced if counts.get(t, 0) >= threshold]
        if not big:
            return CostVerdict(True, "plan unavailable; no large table referenced", estimate)

        has_bound = bool(tree.find(exp.Where)) or bool(tree.find(exp.Limit))
        if has_bound:
            return CostVerdict(True, "plan unavailable; query is bounded", estimate)
        variant, explanation = self.cheaper_variant(sql, big)
        return CostVerdict(
            False,
            f"unbounded query over {', '.join(big)} "
            f"({max(counts.get(t, 0) for t in big):,} rows) and no plan available",
            estimate,
            big,
            variant,
            explanation,
        )

    def cheaper_variant(self, sql: str, tables: list[str]) -> tuple[str | None, str]:
        """Suggest a bounded version of the same query.

        Two moves, in order of preference: add a row limit if there is none, and
        aggregate instead of listing when the query is a bare projection. Anything
        cleverer than that is the user's call, not ours.
        """
        profile = self.connector.profile
        try:
            tree = sqlglot.parse_one(sql, read=profile.sqlglot_dialect)
        except Exception:
            return None, ""
        if not isinstance(tree, exp.Select):
            return None, ""

        has_agg = any(isinstance(n, exp.AggFunc) for n in tree.find_all(exp.AggFunc))
        has_limit = tree.args.get("limit") is not None

        if has_agg:
            return None, ""  # an aggregate has to read the rows; there is no cheaper shape

        if not has_limit and (profile.limit or profile.top):
            return (
                tree.copy().limit(1000).sql(dialect=profile.sqlglot_dialect, pretty=True),
                "Same query, capped at the first 1,000 rows. The full result would "
                f"require reading all of {', '.join(tables)}.",
            )

        counted = tree.copy()
        counted.set("expressions", [exp.Count(this=exp.Star())])
        counted.set("limit", None)
        return (
            counted.sql(dialect=profile.sqlglot_dialect, pretty=True),
            "Count the matching rows first, then decide whether to list them.",
        )
