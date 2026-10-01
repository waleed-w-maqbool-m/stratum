"""Confidence scoring and the gate (PLAN.md 4.4 steps 12-13).

Confidence has to mean something. A number that is high when the system is wrong is
worse than no number at all, because the whole point of the gate is to decide when a
human needs to look. So the score is built from signals that actually correlate with
being right, each one recorded with its contribution so the Lab can show why.

Hand-weighted to begin with. Phase 5 fits a logistic regression on gauntlet runs and
plots the reliability diagram; the weights here are the prior that replaces.

The gate (PLAN.md 4.4 step 13):
    >= 0.80  show the answer
    0.50-0.80  show it with an Assumptions panel and ask "looks right?"
    < 0.50   clarify, or abstain with reasons and the best partial answer
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..semantic.model import SemanticModel
from .joinplan import JoinPlan
from .validate import ValidationResult

SHOW_THRESHOLD = 0.80
ASSUMPTIONS_THRESHOLD = 0.50


@dataclass
class Signal:
    name: str
    value: float          # the raw observation, 0..1
    weight: float         # how much it moves the score
    reason: str = ""

    @property
    def contribution(self) -> float:
        return self.value * self.weight


@dataclass
class ConfidenceScore:
    score: float
    signals: list[Signal] = field(default_factory=list)
    verified: bool = False

    @property
    def band(self) -> str:
        if self.verified or self.score >= SHOW_THRESHOLD:
            return "show"
        if self.score >= ASSUMPTIONS_THRESHOLD:
            return "assumptions"
        return "abstain"

    def reasons(self) -> list[str]:
        """Why the score is what it is, strongest influence first."""
        ordered = sorted(self.signals, key=lambda s: -abs(s.contribution))
        return [s.reason for s in ordered if s.reason][:6]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "band": self.band,
            "verified": self.verified,
            "reasons": self.reasons(),
            "signals": [
                {
                    "name": s.name, "value": round(s.value, 3),
                    "weight": s.weight, "contribution": round(s.contribution, 3),
                    "reason": s.reason,
                }
                for s in self.signals
            ],
        }


def score_answer(
    *,
    model: SemanticModel,
    entities_used: list[str],
    join_plan: JoinPlan | None,
    validation: ValidationResult | None,
    candidate_agreement: float,
    candidates_run: int,
    repairs: int,
    linking_margin: float,
    verified_hit: bool = False,
    executed: bool = True,
    row_count: int = 0,
) -> ConfidenceScore:
    """Combine the evidence into one number.

    A verified query short-circuits to 0.99: it was written or approved by a person for
    this exact question, and nothing the heuristics say should move that.
    """
    if verified_hit:
        return ConfidenceScore(
            score=0.99,
            verified=True,
            signals=[
                Signal("verified", 1.0, 0.99,
                       "this question matched a query a person has already verified")
            ],
        )

    signals: list[Signal] = []

    # --- how much of the model this answer leans on is reviewed rather than guessed
    column_confidences: list[float] = []
    hypothesis_count = 0
    for name in entities_used:
        entity = model.entity(name)
        if entity is None:
            continue
        for column in entity.columns.values():
            if column.status == "accepted":
                column_confidences.append(1.0)
            elif column.status == "hypothesis":
                column_confidences.append(column.confidence)
                hypothesis_count += 1
        for filt in entity.active_filters():
            column_confidences.append(
                1.0 if filt.status == "accepted" else filt.confidence
            )
    mapping_confidence = (
        min(column_confidences) if column_confidences else 0.5
    )
    signals.append(
        Signal(
            "mapping_confidence", mapping_confidence, 0.22,
            (
                f"{hypothesis_count} of the mappings this answer uses are unreviewed "
                "hypotheses"
                if hypothesis_count
                else "every mapping this answer uses has been reviewed"
            ),
        )
    )

    # --- the weakest join the answer travels through
    join_confidence = join_plan.min_confidence if join_plan and join_plan.steps else 1.0
    if join_plan and join_plan.steps:
        weakest = min(join_plan.steps, key=lambda s: s.confidence)
        reason = (
            f"the {weakest.from_entity} to {weakest.to_entity} join is inferred, not "
            f"declared (confidence {weakest.confidence:.2f})"
            if weakest.confidence < 0.9
            else "every join used is high confidence"
        )
    else:
        reason = "no joins were needed"
    signals.append(Signal("join_confidence", join_confidence, 0.20, reason))

    # --- do independently generated candidates agree?
    if candidates_run > 1:
        signals.append(
            Signal(
                "candidate_agreement", candidate_agreement, 0.20,
                (
                    "independently generated queries returned the same result"
                    if candidate_agreement >= 1.0
                    else "independently generated queries disagreed, and one was chosen "
                         "by comparing their results"
                ),
            )
        )
    else:
        signals.append(
            Signal("candidate_agreement", 0.6, 0.10,
                   "only one candidate was generated, so there is nothing to cross-check")
        )

    # --- validation
    if validation is not None:
        failed = validation.blocking
        unknown = [c for c in validation.checks if c.passed is None]
        value = 1.0 if not failed and not unknown else (0.5 if not failed else 0.0)
        signals.append(
            Signal(
                "validation", value, 0.18,
                (
                    "all validation checks passed"
                    if value == 1.0
                    else f"validation could not confirm: "
                         f"{', '.join(c.name for c in (failed or unknown))}"
                ),
            )
        )

    # --- repairs
    repair_penalty = max(0.0, 1.0 - 0.35 * repairs)
    signals.append(
        Signal(
            "repairs", repair_penalty, 0.10,
            (
                f"the query needed {repairs} correction(s) before it ran"
                if repairs else "the query ran first time"
            ),
        )
    )

    # --- how clearly retrieval picked the tables
    signals.append(
        Signal(
            "linking_margin", min(1.0, max(0.0, linking_margin * 4)), 0.10,
            (
                "it was not obvious which tables the question is about"
                if linking_margin < 0.15
                else "the question clearly names which tables it is about"
            ),
        )
    )

    total_weight = sum(s.weight for s in signals)
    raw = sum(s.contribution for s in signals) / total_weight if total_weight else 0.0

    if not executed:
        raw *= 0.5
        signals.append(
            Signal("executed", 0.0, 0.0, "the query was not executed")
        )
    elif row_count == 0:
        # An empty result is sometimes right and often a sign of a wrong filter.
        raw *= 0.85
        signals.append(
            Signal("empty_result", 0.0, 0.0,
                   "the query returned no rows, which may mean a filter is wrong")
        )

    return ConfidenceScore(score=round(min(0.98, max(0.0, raw)), 3), signals=signals)


def gate(score: ConfidenceScore) -> dict[str, Any]:
    """What the UI should do with this answer."""
    band = score.band
    if band == "show":
        return {
            "action": "show",
            "message": "",
        }
    if band == "assumptions":
        return {
            "action": "show_with_assumptions",
            "message": (
                "This answer relies on mappings nobody has reviewed yet. The "
                "assumptions are listed below — please confirm they are right."
            ),
        }
    return {
        "action": "abstain",
        "message": (
            "I am not confident enough in this answer to show it. "
            + (score.reasons()[0] if score.reasons() else "")
        ),
    }
