"""Confidence calibration (PLAN.md 4.4 step 12, Phase 5).

The hand-weighted confidence score is a prior. This replaces it with weights fitted on
actual gauntlet outcomes, and — more importantly — measures whether the resulting number
means anything.

**A confidence score is only useful if it is calibrated.** If answers scored 0.9 are
right 60% of the time, the number is not a weak signal, it is a lie, and the gate built
on it will show wrong answers with a green badge. So the output here is not just weights:
it is the reliability diagram and the expected calibration error, which say how much the
number can be trusted.

Logistic regression by gradient descent, deliberately without scikit-learn: the feature
count is single digits, the dataset is a few dozen rows, and an extra 80 MB of
dependency to avoid thirty lines of arithmetic is a bad trade in a project whose whole
premise is running on a free tier.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The signals `confidence.score_answer` records. Order is fixed so a stored weight
# vector stays meaningful across runs.
FEATURES = [
    "mapping_confidence",
    "join_confidence",
    "candidate_agreement",
    "validation",
    "repairs",
    "linking_margin",
]

# Bins for the reliability diagram. Ten is conventional and readable.
BIN_COUNT = 10


@dataclass
class Observation:
    """One answered question: what the signals said, and whether it was right."""

    question_id: str
    features: dict[str, float]
    predicted: float
    correct: bool

    def vector(self) -> list[float]:
        return [self.features.get(name, 0.0) for name in FEATURES]


@dataclass
class Bin:
    lower: float
    upper: float
    count: int = 0
    predicted_mean: float = 0.0
    observed_accuracy: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "range": [round(self.lower, 2), round(self.upper, 2)],
            "count": self.count,
            "predicted": round(self.predicted_mean, 3),
            "observed": round(self.observed_accuracy, 3),
            "gap": round(self.observed_accuracy - self.predicted_mean, 3),
        }


@dataclass
class Calibration:
    weights: dict[str, float] = field(default_factory=dict)
    intercept: float = 0.0
    observations: int = 0
    ece_before: float = 0.0
    ece_after: float = 0.0
    bins_before: list[Bin] = field(default_factory=list)
    bins_after: list[Bin] = field(default_factory=list)
    monotonic: bool = False
    note: str = ""

    def predict(self, features: dict[str, float]) -> float:
        if not self.weights:
            return 0.0
        z = self.intercept + sum(
            self.weights.get(name, 0.0) * features.get(name, 0.0) for name in FEATURES
        )
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "intercept": round(self.intercept, 4),
            "observations": self.observations,
            "ece_before": round(self.ece_before, 4),
            "ece_after": round(self.ece_after, 4),
            "monotonic": self.monotonic,
            "reliability_before": [b.to_dict() for b in self.bins_before],
            "reliability_after": [b.to_dict() for b in self.bins_after],
            "note": self.note,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), "utf-8")

    @classmethod
    def load(cls, path: Path) -> Calibration | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text("utf-8"))
        return cls(
            weights=raw.get("weights", {}),
            intercept=raw.get("intercept", 0.0),
            observations=raw.get("observations", 0),
            ece_before=raw.get("ece_before", 0.0),
            ece_after=raw.get("ece_after", 0.0),
            monotonic=raw.get("monotonic", False),
            note=raw.get("note", ""),
        )


# ------------------------------------------------------------------------ measuring


def reliability(observations: list[Observation], predictor=None) -> tuple[list[Bin], float]:
    """The reliability diagram and the expected calibration error.

    ECE is the average gap between what confidence claimed and what actually happened,
    weighted by how many answers fell in each bin. Zero means the number is honest.
    """
    bins = [
        Bin(lower=index / BIN_COUNT, upper=(index + 1) / BIN_COUNT)
        for index in range(BIN_COUNT)
    ]
    grouped: list[list[Observation]] = [[] for _ in bins]

    for observation in observations:
        score = (
            predictor(observation.features) if predictor else observation.predicted
        )
        index = min(BIN_COUNT - 1, max(0, int(score * BIN_COUNT)))
        grouped[index].append(observation)
        bins[index].predicted_mean += score

    total = len(observations)
    ece = 0.0
    for index, bucket in enumerate(grouped):
        if not bucket:
            continue
        bins[index].count = len(bucket)
        bins[index].predicted_mean /= len(bucket)
        bins[index].observed_accuracy = sum(o.correct for o in bucket) / len(bucket)
        ece += (
            len(bucket) / total
            * abs(bins[index].observed_accuracy - bins[index].predicted_mean)
        )
    return bins, ece


def is_monotonic(bins: list[Bin], tolerance: float = 0.12) -> bool:
    """Does accuracy rise with confidence?

    The Phase 5 acceptance criterion. Weaker than calibration and more important: a
    score can be badly calibrated and still useful for *ranking*, but one where higher
    confidence does not mean more often right is worse than no score, because the gate
    would then be actively misleading.

    Empty bins are skipped, and a small dip is tolerated — with forty questions a
    single answer moves a bin by a quarter.
    """
    populated = [b for b in bins if b.count > 0]
    if len(populated) < 2:
        return False
    for earlier, later in zip(populated, populated[1:], strict=False):
        if later.observed_accuracy < earlier.observed_accuracy - tolerance:
            return False
    return True


# -------------------------------------------------------------------------- fitting


def fit(
    observations: list[Observation],
    *,
    iterations: int = 4000,
    learning_rate: float = 0.35,
    l2: float = 0.02,
) -> Calibration:
    """Fit a logistic regression on the recorded signals.

    L2 regularisation is not optional here: with a few dozen observations and six
    features, an unregularised fit will happily drive one weight to infinity on a
    coincidence and produce a model that is confident and wrong about everything new.
    """
    result = Calibration(observations=len(observations))
    if len(observations) < 12:
        result.note = (
            f"only {len(observations)} scored answers — too few to fit anything "
            "meaningful. The hand-weighted prior is still in use."
        )
        bins, ece = reliability(observations)
        result.bins_before = result.bins_after = bins
        result.ece_before = result.ece_after = ece
        result.monotonic = is_monotonic(bins)
        return result

    vectors = [o.vector() for o in observations]
    labels = [1.0 if o.correct else 0.0 for o in observations]
    n_features = len(FEATURES)
    weights = [0.0] * n_features
    intercept = 0.0
    n = float(len(observations))

    for _ in range(iterations):
        gradient = [0.0] * n_features
        gradient_intercept = 0.0
        for vector, label in zip(vectors, labels, strict=False):
            z = intercept + sum(w * x for w, x in zip(weights, vector, strict=False))
            prediction = 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))
            error = prediction - label
            for index, x in enumerate(vector):
                gradient[index] += error * x
            gradient_intercept += error
        for index in range(n_features):
            # The L2 term is applied to the weights only, never the intercept: the
            # intercept carries the base rate and shrinking it biases every prediction.
            weights[index] -= learning_rate * (
                gradient[index] / n + l2 * weights[index]
            )
        intercept -= learning_rate * gradient_intercept / n

    result.weights = dict(zip(FEATURES, weights, strict=False))
    result.intercept = intercept

    result.bins_before, result.ece_before = reliability(observations)
    result.bins_after, result.ece_after = reliability(observations, result.predict)
    result.monotonic = is_monotonic(result.bins_after)
    result.note = (
        f"fitted on {len(observations)} answers; calibration error "
        f"{result.ece_before:.3f} -> {result.ece_after:.3f}"
    )
    return result


# ------------------------------------------------------------- reading an eval run


def observations_from_run(
    answers_path: Path, outcomes: dict[str, bool]
) -> list[Observation]:
    """Pair each answer's recorded signals with whether the scorer marked it correct.

    `outcomes` comes from the scorer, which is the only thing allowed to decide what
    "correct" means. Calibration never looks at gold SQL itself.
    """
    if not answers_path.exists():
        return []
    out: list[Observation] = []
    for line in answers_path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        question_id = record.get("id")
        answer = record.get("answer") or {}
        confidence = answer.get("confidence")
        if question_id not in outcomes or not confidence:
            continue
        features = {
            signal["name"]: float(signal.get("value", 0.0))
            for signal in confidence.get("signals", [])
        }
        if not features:
            continue
        out.append(
            Observation(
                question_id=question_id,
                features=features,
                predicted=float(confidence.get("score", 0.0)),
                correct=bool(outcomes[question_id]),
            )
        )
    return out


def render_reliability(calibration: Calibration) -> str:
    """The reliability diagram as text, for the terminal and for PROGRESS.md."""
    lines = [
        f"reliability ({calibration.observations} answers, "
        f"ECE {calibration.ece_before:.3f} -> {calibration.ece_after:.3f})",
        "",
        "  confidence    n   claimed   actual   gap",
    ]
    for bucket in calibration.bins_after:
        if not bucket.count:
            continue
        gap = bucket.observed_accuracy - bucket.predicted_mean
        marker = "ok " if abs(gap) <= 0.1 else ("over" if gap < 0 else "under")
        lines.append(
            f"  {bucket.lower:.1f}-{bucket.upper:.1f}  {bucket.count:>3}"
            f"    {bucket.predicted_mean:>5.2f}    {bucket.observed_accuracy:>5.2f}"
            f"  {gap:>+5.2f}  {marker}"
        )
    lines.append("")
    lines.append(
        "accuracy rises with confidence"
        if calibration.monotonic
        else "WARNING: accuracy does not rise with confidence — the gate would mislead"
    )
    return "\n".join(lines)
