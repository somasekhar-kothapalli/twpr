"""Unit tests for the TWPR 5-step rule engine.

The engine must be 100% deterministic: the same inputs always produce the same
grade, direction, confidence and trade recommendation. Real money depends on it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from signal_engine import generate_signal  # noqa: E402


def signal(
    crude: float,
    *,
    gasoline: float = 0.0,
    distillate: float = 0.0,
    cushing: float = 0.0,
    api: float = 0.0,
):
    """Build a signal with only the inputs a test cares about."""
    return generate_signal(
        crude_deviation_mb=crude,
        gasoline_deviation_mb=gasoline,
        distillate_deviation_mb=distillate,
        cushing_mb=cushing,
        api_crude_mb=api,
    )


# --- Step 1 — skip zone ---------------------------------------------------


def test_skip_zero_deviation():
    result = signal(0.0)
    assert result.grade == "skip"
    assert result.direction == "neutral"
    assert result.confidence == 0
    assert result.option_type is None


def test_skip_just_under_threshold():
    assert signal(0.99).grade == "skip"
    assert signal(-0.99).grade == "skip"


def test_skip_exactly_threshold():
    assert signal(1.0).grade == "skip"
    assert signal(-1.0).grade == "skip"


# --- Step 1 — grade determination -----------------------------------------


def test_grade_b_bullish():
    result = signal(-1.2)
    assert (result.grade, result.direction) == ("B", "bullish")
    assert (result.option_type, result.strike_type, result.size_pct) == ("call", "1-OTM", 1.5)


def test_grade_a_bullish():
    result = signal(-1.6)
    assert (result.grade, result.direction) == ("A", "bullish")
    assert (result.option_type, result.strike_type, result.size_pct) == ("call", "ATM", 2.0)


def test_grade_b_bearish():
    result = signal(1.2)
    assert (result.grade, result.direction) == ("B", "bearish")
    assert (result.option_type, result.strike_type, result.size_pct) == ("put", "1-OTM", 1.5)


def test_grade_a_bearish():
    result = signal(1.6)
    assert (result.grade, result.direction) == ("A", "bearish")
    assert (result.option_type, result.strike_type, result.size_pct) == ("put", "ATM", 2.0)


def test_grade_a_boundary_is_inclusive():
    """Exactly +/-1.5 is Grade A, not B."""
    assert signal(1.5).grade == "A"
    assert signal(-1.5).grade == "A"


# --- Step 2 — Cushing adjustment ------------------------------------------


def test_cushing_confirms_bullish():
    """Bullish crude + Cushing draw: Grade A survives."""
    result = signal(-1.6, cushing=-0.5)
    assert result.grade == "A"
    assert result.cushing_confirms is True


def test_cushing_contradicts_bullish():
    """Bullish crude + Cushing build: A downgrades to B."""
    result = signal(-1.6, cushing=0.5)
    assert result.grade == "B"
    assert result.direction == "bullish"
    assert result.cushing_confirms is False


def test_cushing_contradicts_bearish():
    """Bearish crude + Cushing draw: A downgrades to B."""
    result = signal(1.6, cushing=-0.5)
    assert result.grade == "B"
    assert result.direction == "bearish"
    assert result.cushing_confirms is False


def test_b_stays_b_when_contradicted():
    """B is the floor — a contradicting Cushing never downgrades it further."""
    result = signal(1.2, cushing=-0.5)
    assert result.grade == "B"
    assert result.cushing_confirms is False


def test_flat_cushing_counts_as_confirming():
    """Cushing of exactly 0.0 contradicts nothing."""
    assert signal(1.6, cushing=0.0).cushing_confirms is True


# --- Step 3 — products check ----------------------------------------------


def test_products_strongly_oppose_flagged_but_grade_unchanged():
    result = signal(1.6, gasoline=2.5, distillate=2.5, cushing=1.0)
    assert result.products_strongly_oppose is True
    assert result.grade == "A"  # products never change the grade


def test_products_below_threshold_not_flagged():
    assert signal(1.6, gasoline=1.9, distillate=2.5).products_strongly_oppose is False


# --- Step 4 — API alignment -----------------------------------------------


def test_api_aligns_adds_confidence():
    """Bearish + API build aligns: +5 over the contradicting case's -5, so +10 apart."""
    aligned = signal(1.2, cushing=1.0, api=1.5)
    contradicting = signal(1.2, cushing=1.0, api=-1.5)
    assert aligned.api_aligns is True
    assert aligned.confidence - contradicting.confidence == 10


def test_api_contradicts_reduces_confidence():
    result = signal(1.2, cushing=1.0, api=-1.5)
    assert result.api_aligns is False
    assert result.confidence == 55  # 55 base + 5 Cushing - 5 API


def test_api_bullish_alignment_is_a_draw():
    assert signal(-1.2, api=-1.5).api_aligns is True
    assert signal(-1.2, api=1.5).api_aligns is False


# --- Step 5 — confidence ---------------------------------------------------


def test_grade_a_base_confidence():
    """Grade A, Cushing confirms (+5), API contradicts (-5) = the 75 base."""
    assert signal(1.6, cushing=1.0, api=-1.0).confidence == 75


def test_grade_b_base_confidence():
    """Grade B, Cushing confirms (+5), API contradicts (-5) = the 55 base."""
    assert signal(1.2, cushing=1.0, api=-1.0).confidence == 55


def test_max_confidence():
    result = signal(1.6, cushing=1.0, api=1.0)
    assert result.grade == "A"
    assert result.confidence == 85


def test_min_tradeable_confidence():
    result = signal(1.2, cushing=-1.0, api=-1.0)
    assert result.grade == "B"
    assert result.confidence == 45


def test_confidence_stays_in_range_across_the_grid():
    """No input combination may escape 45-85 for a tradeable signal."""
    for crude in (-2.0, -1.6, -1.2, 1.2, 1.6, 2.0):
        for cushing in (-1.0, 0.0, 1.0):
            for api in (-1.0, 0.0, 1.0):
                result = signal(crude, cushing=cushing, api=api)
                assert 45 <= result.confidence <= 85, result


# --- Determinism -----------------------------------------------------------


def test_engine_is_deterministic():
    args = dict(gasoline=2.669, distillate=2.787, cushing=-0.684, api=1.250)
    first = signal(1.209, **args)
    for _ in range(20):
        assert signal(1.209, **args) == first


# --- Reference week — Sep 4, 2026 -----------------------------------------


def test_reference_week_sep4_2026():
    """End-to-end validation against the known-good week."""
    result = signal(1.209, gasoline=2.669, distillate=2.787, cushing=-0.684, api=1.250)
    assert result.grade == "B"
    assert result.direction == "bearish"
    assert result.confidence == 55
    assert result.option_type == "put"
    assert result.strike_type == "1-OTM"
    assert result.size_pct == 1.5
    assert result.cushing_confirms is False  # Cushing draw contradicts bearish
    assert result.api_aligns is True  # API build aligns with bearish


def test_reference_week_matches_committed_fixture():
    """data/reference_week.json is the same case, kept in sync with the engine."""
    fixture = json.loads(
        (Path(__file__).parent.parent / "data" / "reference_week.json").read_text(encoding="utf-8")
    )
    inputs, expected = fixture["inputs"], fixture["expected"]
    deviations = inputs["deviations"]

    # The crude deviation in the fixture must follow from its own raw EIA/consensus pair.
    computed_crude = inputs["crude"]["eia_crude_change_mb"] - inputs["crude"]["consensus_crude_mb"]
    assert computed_crude == pytest.approx(deviations["crude_deviation_mb"], abs=1e-9)

    result = generate_signal(
        crude_deviation_mb=computed_crude,
        gasoline_deviation_mb=deviations["gasoline_deviation_mb"],
        distillate_deviation_mb=deviations["distillate_deviation_mb"],
        cushing_mb=inputs["cushing_mb"],
        api_crude_mb=inputs["api_crude_mb"],
    )

    assert result.crude_deviation_mb == pytest.approx(expected["crude_deviation_mb"], abs=1e-9)
    assert result.grade == expected["grade"]
    assert result.direction == expected["direction"]
    assert result.confidence == expected["confidence"]
    assert result.option_type == expected["option_type"]
    assert result.strike_type == expected["strike_type"]
    assert result.size_pct == expected["size_pct"]
    assert result.cushing_confirms == expected["cushing_confirms"]
    assert result.api_aligns == expected["api_aligns"]
    assert result.products_strongly_oppose == expected["products_strongly_oppose"]
