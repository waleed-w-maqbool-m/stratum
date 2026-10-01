"""The scorer is the instrument, so its own errors are invisible in every report.

A bug here does not look like a bug. It looks like the system being worse than it is,
spread thinly across a results table, which is exactly the kind of number nobody
questions. These cases pin down what `compare` is supposed to accept and reject.

The case that prompted the file: "List each employee with the name of their manager"
returned all eight employees paired with the right manager and was scored wrong for
also selecting the employee id, even though `compare` documents extra columns as
allowed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gauntlet.scorer import compare  # noqa: E402


# --------------------------------------------------------------- what must be accepted


def test_extra_system_columns_are_allowed():
    """The real c-039: gold asks for two names, the system also returns ids."""
    gold = [
        ("Adams", None), ("Callahan", "Mitchell"), ("Edwards", "Adams"),
        ("Johnson", "Edwards"), ("King", "Mitchell"), ("Mitchell", "Adams"),
        ("Park", "Edwards"), ("Peacock", "Edwards"),
    ]
    system = [
        [1, "Andrew", "Adams", None, None],
        [2, "Nancy", "Edwards", "Andrew", "Adams"],
        [3, "Jane", "Peacock", "Nancy", "Edwards"],
        [4, "Margaret", "Park", "Nancy", "Edwards"],
        [5, "Steve", "Johnson", "Nancy", "Edwards"],
        [6, "Michael", "Mitchell", "Andrew", "Adams"],
        [7, "Robert", "King", "Michael", "Mitchell"],
        [8, "Laura", "Callahan", "Michael", "Mitchell"],
    ]
    result = compare(gold, system)
    assert result.correct, result.reason
    assert result.matched_columns == 2


def test_column_order_does_not_matter():
    assert compare([("a", 1), ("b", 2)], [[1, "a"], [2, "b"]]).correct


def test_row_order_does_not_matter_by_default():
    assert compare([("a", 1), ("b", 2)], [["b", 2], ["a", 1]]).correct


# --------------------------------------------------------------- what must be rejected


def test_values_paired_wrongly_across_rows_are_caught():
    """Both columns hold the right values; the pairing between them is wrong.

    This is the case the alignment guard exists for, and narrowing that guard to the
    matched columns must not blind it.
    """
    result = compare([("a", 1), ("b", 2)], [["a", 2], ["b", 1]])
    assert not result.correct
    assert "do not align" in result.reason


def test_a_wrong_value_is_caught():
    assert not compare([("a", 1), ("b", 2)], [["a", 1], ["b", 3]]).correct


def test_a_missing_gold_column_is_caught():
    """Returning only half of what was asked for is not a pass."""
    assert not compare([("a", 1), ("b", 2)], [["a"], ["b"]]).correct


def test_row_count_must_agree():
    result = compare([("a",), ("b",)], [["a"]])
    assert not result.correct
    assert "row count" in result.reason


def test_extra_columns_cannot_smuggle_in_a_wrong_pairing():
    """An extra column must not let a mispaired answer through the narrowed guard."""
    gold = [("a", 1), ("b", 2)]
    system = [["x", "a", 2], ["y", "b", 1]]
    assert not compare(gold, system).correct


# ------------------------------------------------------------------------ order-sensitive


def test_order_sensitive_rejects_a_reordering():
    assert not compare([("a",), ("b",)], [["b"], ["a"]], order_sensitive=True).correct
    assert compare([("a",), ("b",)], [["a"], ["b"]], order_sensitive=True).correct


@pytest.mark.parametrize("rows", [[], [()]])
def test_empty_results_do_not_crash(rows):
    compare(rows, rows)
