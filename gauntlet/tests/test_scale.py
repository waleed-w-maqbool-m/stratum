"""The scale benchmark's own build, on a size small enough to run with the tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gauntlet import scale  # noqa: E402


def test_a_small_synthetic_legacy_schema_is_recovered_with_no_false_edges():
    scale.BUILD.mkdir(exist_ok=True)
    report = scale.run(tables=20, rows=150)
    assert report["true_relationships"] > 10
    assert report["join_precision"] == 1.0
    assert report["join_recall"] >= 0.9
