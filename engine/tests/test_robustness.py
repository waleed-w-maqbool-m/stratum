"""Phase 5: the cache, calibration, the budgeter and the fallback ladder.

These are the parts that decide what happens when something goes wrong — a rate limit, a
dialect that cannot express the query, a model that is confidently mistaken. They are
worth testing precisely because their failure mode is silence.
"""

from __future__ import annotations

import time

from stratum.ask.cache import SemanticCache
from stratum.ask.calibration import (
    Observation,
    fit,
    is_monotonic,
    reliability,
)
from stratum.ask.confidence import ASSUMPTIONS_THRESHOLD, SHOW_THRESHOLD, gate, score_answer
from stratum.llm.budget import Budgeter, ProviderBudget, QuestionBudget, estimate_tokens

# ---------------------------------------------------------------------- the cache


def test_cache_returns_a_paraphrase():
    cache = SemanticCache()
    cache.put("How many customers do we have?", "SELECT COUNT(*) FROM customer", 1,
              confidence=0.9)
    assert cache.lookup("how many customers do we have?", 1) is not None


def test_cache_is_invalidated_by_a_model_change():
    """A model change is exactly the thing that would alter the answer."""
    cache = SemanticCache()
    cache.put("How many customers?", "SELECT COUNT(*) FROM customer", 1, confidence=0.9)
    assert cache.lookup("How many customers?", 1) is not None
    assert cache.lookup("How many customers?", 2) is None, (
        "version 2 of the model may map the same question differently"
    )


def test_cache_expires():
    cache = SemanticCache(ttl=0)
    cache.put("How many customers?", "SELECT COUNT(*) FROM customer", 1, confidence=0.9)
    time.sleep(0.01)
    assert cache.lookup("How many customers?", 1) is None, (
        "the underlying data is live; a stale count is a wrong count"
    )


def test_cache_refuses_to_remember_a_low_confidence_answer():
    cache = SemanticCache()
    cache.put("something vague", "SELECT 1", 1, confidence=0.3)
    assert len(cache) == 0, "repeating a doubtful answer faster is not an improvement"


def test_cache_evicts_oldest_first():
    cache = SemanticCache(max_entries=3)
    for index in range(5):
        cache.put(f"question {index}", f"SELECT {index}", 1, confidence=0.9)
    assert len(cache) == 3
    assert cache.lookup("question 0", 1) is None
    assert cache.lookup("question 4", 1) is not None


# ------------------------------------------------------------------- calibration


def _observation(mapping: float, joins: float, correct: bool, predicted: float = 0.85):
    return Observation(
        question_id=f"q{mapping}-{joins}-{correct}",
        features={
            "mapping_confidence": mapping,
            "join_confidence": joins,
            "candidate_agreement": 1.0,
            "validation": 1.0,
            "repairs": 1.0,
            "linking_margin": 0.5,
        },
        predicted=predicted,
        correct=correct,
    )


def test_calibration_refuses_to_fit_on_too_few_observations():
    result = fit([_observation(0.9, 0.9, True) for _ in range(5)])
    assert not result.weights
    assert "too few" in result.note


def test_calibration_improves_a_miscalibrated_score():
    """A score that always claims 0.85 while being right half the time is a lie."""
    import random

    random.seed(11)
    observations = []
    for _index in range(150):
        mapping = random.random()
        joins = random.random()
        probability = min(0.95, max(0.05, 0.1 + 0.6 * mapping + 0.25 * joins))
        observations.append(
            _observation(mapping, joins, random.random() < probability, predicted=0.85)
        )
    result = fit(observations)
    assert result.ece_after < result.ece_before, (
        f"calibration should reduce the error, got "
        f"{result.ece_before:.3f} -> {result.ece_after:.3f}"
    )
    assert result.ece_after < 0.1
    assert result.monotonic


def test_reliability_detects_a_non_monotonic_score():
    """The Phase 5 criterion. A score where confidence does not track accuracy is
    worse than no score, because the gate built on it actively misleads."""
    observations = (
        [_observation(0.2, 0.2, True, predicted=0.25) for _ in range(20)]
        + [_observation(0.9, 0.9, False, predicted=0.95) for _ in range(20)]
    )
    bins, _ece = reliability(observations)
    assert not is_monotonic(bins), "high confidence was always wrong here"


def test_reliability_accepts_a_monotonic_score():
    observations = (
        [_observation(0.2, 0.2, False, predicted=0.25) for _ in range(18)]
        + [_observation(0.2, 0.2, True, predicted=0.25) for _ in range(2)]
        + [_observation(0.9, 0.9, True, predicted=0.95) for _ in range(19)]
        + [_observation(0.9, 0.9, False, predicted=0.95) for _ in range(1)]
    )
    bins, _ece = reliability(observations)
    assert is_monotonic(bins)


# ----------------------------------------------------------------- the budgeter


def test_question_budget_is_a_ceiling_not_a_target():
    budget = QuestionBudget(limit=6000)
    budget.charge("generate", 5800)
    assert not budget.can_afford(500)
    assert budget.remaining == 200


def test_provider_budget_reports_how_long_to_wait():
    provider = ProviderBudget("test", tokens_per_minute=8000, requests_per_minute=30)
    provider.record(7500)
    assert not provider.available(1000)
    wait = provider.wait_time(1000)
    assert 0 < wait <= 61, f"should wait out the minute window, got {wait}"


def test_budgeter_picks_an_available_provider():
    fast = ProviderBudget("fast", tokens_per_minute=100)
    roomy = ProviderBudget("roomy", tokens_per_minute=100000)
    budgeter = Budgeter({"fast": fast, "roomy": roomy})
    fast.record(99)
    name, wait = budgeter.pick_provider(["fast", "roomy"], 500)
    assert name == "roomy"
    assert wait == 0


def test_token_estimate_errs_high():
    """Erring low is the dangerous direction: it means discovering the ceiling by
    breaching it mid-question."""
    text = "SELECT COUNT(*) FROM customer WHERE country = 'USA'"
    assert estimate_tokens(text) >= len(text) / 5


# ------------------------------------------------------------------- the gate


def test_gate_thresholds_match_the_plan():
    assert SHOW_THRESHOLD == 0.80
    assert ASSUMPTIONS_THRESHOLD == 0.50


def test_verified_answer_short_circuits_to_high_confidence(model):
    score = score_answer(
        model=model, entities_used=["customer"], join_plan=None, validation=None,
        candidate_agreement=1.0, candidates_run=1, repairs=0, linking_margin=1.0,
        verified_hit=True, row_count=1,
    )
    assert score.verified
    assert score.score >= 0.99
    assert gate(score)["action"] == "show"


def test_an_empty_result_lowers_confidence(model):
    common = dict(
        model=model, entities_used=["customer"], join_plan=None, validation=None,
        candidate_agreement=1.0, candidates_run=2, repairs=0, linking_margin=1.0,
    )
    with_rows = score_answer(**common, row_count=12)
    without = score_answer(**common, row_count=0)
    assert without.score < with_rows.score, (
        "an empty result is sometimes right and often a wrong filter"
    )


def test_repairs_lower_confidence(model):
    common = dict(
        model=model, entities_used=["customer"], join_plan=None, validation=None,
        candidate_agreement=1.0, candidates_run=2, linking_margin=1.0, row_count=5,
    )
    clean = score_answer(**common, repairs=0)
    repaired = score_answer(**common, repairs=2)
    assert repaired.score < clean.score


def test_confidence_reasons_are_human_readable(model):
    score = score_answer(
        model=model, entities_used=["customer"], join_plan=None, validation=None,
        candidate_agreement=1.0, candidates_run=2, repairs=0, linking_margin=0.05,
        row_count=5,
    )
    reasons = score.reasons()
    assert reasons
    assert all(len(r) > 12 for r in reasons), "a reason should be a sentence, not a label"


def test_a_named_measure_settles_a_superlative(model):
    """"The largest single invoice total" was sent back asking which column "largest"
    meant, with the answer already in the question.

    Two things are open in a superlative and they resolve independently: the measure
    (named here as a bare noun rather than after "by"), and the cut-off (which a
    singular superlative fixes at one row).
    """
    from stratum.ask.ambiguity import detect

    assert not detect("What was the largest single invoice total?", model).ambiguities
    assert not detect("What is the longest track duration?", model).ambiguities
    assert not detect("Top 5 customers by revenue", model).ambiguities


def test_a_genuinely_vague_superlative_still_clarifies(model):
    """The widened hints must not silence the questions clarification exists for."""
    from stratum.ask.ambiguity import detect

    for question in ("Who are our top customers?", "What is our best selling genre?"):
        terms = [a.term for a in detect(question, model).ambiguities]
        assert terms, f"{question!r} should still be ambiguous"


# ------------------------------------------------------------------- name scoring at scale


def test_skipping_the_fuzzy_tiebreak_never_changes_a_score_that_can_clear_the_bar():
    """`fuzzy=False` exists for speed: it must agree with the full score wherever it matters.

    Candidate generation discards same-type pairs scoring under 0.5. The fuzzy fallback is
    capped at 0.45, so for those pairs it can only ever move a score between two values
    that are both discarded. This pins that: any pair that clears 0.5 scores the same both
    ways, and every pair that does not scores under 0.5 both ways.
    """
    from stratum.excavate.naming import fk_name_score

    columns = ["CUST_NO", "CUST_ID", "REP_NO", "MGR_ID", "PRD_ID", "REC_ID", "ORD_NO", "STORE_CD",
               "tblPrd_ID", "strCustNm", "EMP_ID", "ALBM_ID", "TRK_ID", "PLST_ID", "X"]
    tables = ["CUST_MSTR", "EMP_MSTR", "tblPrd", "ALBM", "TRANS_MSTR", "PLST", "SO_LN", "t"]
    for child in columns:
        for parent_column in columns:
            for table in tables:
                full = fk_name_score(child, table, parent_column)
                quick = fk_name_score(child, table, parent_column, fuzzy=False)
                if full >= 0.5:
                    assert quick == full, (child, table, parent_column)
                else:
                    assert quick < 0.5 and full <= 0.45, (child, table, parent_column)
