"""Tests for the INR currency context.

The whole point of this module is that it informs without deciding, so the last
test here is the one that matters: currency must never move grade, direction or
confidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import currency as cx  # noqa: E402
from signal_engine import generate_signal  # noqa: E402

MARKET = {
    "date": "2026-09-27",
    "wti_close": 92.41,
    "usd_inr_close": 95.802,
    "mcx_close": 8853.06,
    "usd_inr_trend_pct": 0.094,
    "wti_trend_pct": -2.305,
}


# --- trend_pct -------------------------------------------------------------


def test_trend_is_oldest_to_newest():
    assert cx.trend_pct([100.0, 101.0, 102.0]) == 2.0
    assert cx.trend_pct([102.0, 100.0]) == pytest.approx(-1.961, abs=1e-3)


def test_trend_skips_gaps():
    """A holiday leaves a None in the window; the trend still spans it."""
    assert cx.trend_pct([100.0, None, 101.0]) == 1.0


@pytest.mark.parametrize("series", [[], [100.0], [None, None], [0.0, 5.0]])
def test_trend_is_none_when_it_cannot_be_computed(series):
    assert cx.trend_pct(series) is None


# --- classify --------------------------------------------------------------


@pytest.mark.parametrize(
    ("trend", "expected"),
    [
        (1.2, "inr_weakening"),   # USD/INR up = rupee weaker
        (0.11, "inr_weakening"),
        (0.09, "flat"),           # under the noise floor
        (0.0, "flat"),
        (-0.09, "flat"),
        (-0.5, "inr_strengthening"),
        (None, "unknown"),
    ],
)
def test_classify(trend, expected):
    assert cx.classify(trend) == expected


# --- effect_on: the sign convention that invites mistakes ------------------


@pytest.mark.parametrize(
    ("direction", "currency_direction", "expected"),
    [
        # A weakening rupee lifts the INR price of crude.
        ("bullish", "inr_weakening", "amplifies"),      # push up, want up
        ("bullish", "inr_strengthening", "dampens"),    # push down, want up
        ("bearish", "inr_weakening", "dampens"),        # push up, want down
        ("bearish", "inr_strengthening", "amplifies"),  # push down, want down
        ("bullish", "flat", "neutral"),
        ("bearish", "unknown", "neutral"),
        ("neutral", "inr_weakening", "neutral"),
    ],
)
def test_effect_on(direction, currency_direction, expected):
    assert cx.effect_on(direction, currency_direction) == expected


# --- strikes ---------------------------------------------------------------


def test_otm_put_sits_below_and_otm_call_above():
    assert cx.strikes(8853.06, "put") == {"strike_atm": 8850, "strike_1_otm": 8800}
    assert cx.strikes(8853.06, "call") == {"strike_atm": 8850, "strike_1_otm": 8900}


def test_skip_signal_has_no_otm_strike():
    assert cx.strikes(8853.06, None)["strike_1_otm"] is None


def test_atm_rounds_to_the_nearest_interval():
    assert cx.strikes(8874.9, "put")["strike_atm"] == 8850
    assert cx.strikes(8875.1, "put")["strike_atm"] == 8900
    assert cx.strikes(8850.0, "put")["strike_atm"] == 8850


def test_strikes_land_on_the_interval():
    for level in (5123.4, 7777.7, 8853.06, 9999.9):
        result = cx.strikes(level, "call")
        assert result["strike_atm"] % cx.STRIKE_INTERVAL == 0
        assert result["strike_1_otm"] % cx.STRIKE_INTERVAL == 0


# --- context ---------------------------------------------------------------


def test_context_from_live_shaped_market_data():
    block = cx.context(MARKET, "bearish", "put")
    assert block["usd_inr_close"] == 95.802
    assert block["currency_direction"] == "flat"
    assert block["currency_effect"] == "neutral"
    assert block["mcx_implied_level"] == 8853.06
    assert block["strike_atm"] == 8850
    assert block["strike_1_otm"] == 8800
    assert block["market_data_date"] == "2026-09-27"


def test_context_degrades_without_market_data():
    """No market data must weaken the guidance, never break the signal."""
    block = cx.context(None, "bearish", "put")
    assert block["strike_atm"] is None
    assert block["currency_direction"] == "unknown"
    assert block["currency_effect"] == "neutral"
    assert "No USD/INR data" in cx.risk_notes(block, "bearish")[0]


def test_context_without_an_mcx_level_still_returns_a_block():
    block = cx.context({**MARKET, "mcx_close": None}, "bearish", "put")
    assert block["strike_atm"] is None
    assert block["usd_inr_close"] == 95.802  # what is known is still reported


# --- risk notes ------------------------------------------------------------


def test_adverse_currency_is_always_flagged():
    block = cx.context({**MARKET, "usd_inr_trend_pct": 0.3}, "bearish", "put")
    notes = cx.risk_notes(block, "bearish")
    assert any("working against" in n for n in notes)


def test_a_sharp_move_adds_the_closed_market_note():
    block = cx.context({**MARKET, "usd_inr_trend_pct": 0.8}, "bearish", "put")
    notes = cx.risk_notes(block, "bearish")
    assert any("sharply" in n for n in notes)
    assert any("closed 17:00-09:00 IST" in n for n in notes)


def test_a_mild_tailwind_is_not_worth_a_note():
    block = cx.context({**MARKET, "usd_inr_trend_pct": -0.3}, "bearish", "put")
    assert cx.risk_notes(block, "bearish") == []


def test_a_strong_tailwind_is_noted_as_borrowed():
    block = cx.context({**MARKET, "usd_inr_trend_pct": -0.8}, "bearish", "put")
    notes = cx.risk_notes(block, "bearish")
    assert any("can give it back" in n for n in notes)


# --- the invariant ---------------------------------------------------------


def test_currency_cannot_move_the_rule_engine():
    """generate_signal takes no currency input at all, by design.

    The decision was context-and-strike only: the FX market is shut during the
    20:00-22:30 IST hold, and over six months the rupee flipped the sign of the
    MCX move versus WTI on 3.2% of days. If currency ever needs to move a number,
    it goes in the spec and the reference week first.
    """
    import inspect

    params = set(inspect.signature(generate_signal).parameters)
    assert params == {
        "crude_deviation_mb",
        "gasoline_deviation_mb",
        "distillate_deviation_mb",
        "cushing_mb",
        "api_crude_mb",
    }
    assert not any("inr" in p or "currency" in p or "fx" in p for p in params)


def test_reference_week_is_unchanged_by_this_feature():
    signal = generate_signal(1.209, 2.669, 2.787, -0.684, 1.250)
    assert (signal.grade, signal.direction, signal.confidence) == ("B", "bearish", 55)
