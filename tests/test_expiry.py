"""Tests for the option expiry calendar and the days-to-expiry gate.

Offline: the calendar is injected rather than fetched. The live-chain check lives
in tests/test_strike_interval.py behind the `network` marker.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import expiry as ex  # noqa: E402
from signal_engine import Signal, apply_expiry_gate, generate_signal  # noqa: E402

# Observed live 2026-09-27: monthly, mid-month.
CALENDAR = ["2026-10-15", "2026-11-17", "2026-12-16"]


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    """Point expiry at a temp cache holding CALENDAR, with fetching disabled."""
    monkeypatch.setattr(ex, "EXPIRIES_FILE", tmp_path / "expiries.json")

    def blocked():
        raise AssertionError("no test may hit the network")

    monkeypatch.setattr(ex, "fetch_expiries", blocked)
    ex.write_json(
        ex.EXPIRIES_FILE,
        {"expiries": CALENDAR, "source": "test", "fetched_at": "2026-09-27T00:00:00+00:00"},
    )


# --- is_tradeable: the boundary --------------------------------------------


@pytest.mark.parametrize(
    ("days", "tradeable"),
    [(0, False), (1, False), (2, False), (3, True), (4, True), (30, True)],
)
def test_is_tradeable_at_the_boundary(days, tradeable):
    """The floor is inclusive: exactly MIN_DAYS_TO_EXPIRY still trades."""
    assert ex.MIN_DAYS_TO_EXPIRY == 3
    assert ex.is_tradeable(days) is tradeable


def test_unknown_days_does_not_block_the_trade():
    """A cache miss is infrastructure, not a reason to cancel a week silently."""
    assert ex.is_tradeable(None) is True


# --- next_expiry / days_to_expiry -----------------------------------------


@pytest.mark.parametrize(
    ("today", "expected_expiry", "expected_days"),
    [
        (date(2026, 9, 27), date(2026, 10, 15), 18),
        (date(2026, 10, 12), date(2026, 10, 15), 3),   # exactly at the floor
        (date(2026, 10, 13), date(2026, 10, 15), 2),   # under it
        (date(2026, 10, 15), date(2026, 10, 15), 0),   # expiry day itself
        (date(2026, 10, 16), date(2026, 11, 17), 32),  # rolls to the next
    ],
)
def test_days_to_expiry(calendar, today, expected_expiry, expected_days):
    assert ex.days_to_expiry(today, allow_fetch=False) == (expected_expiry, expected_days)


def test_no_calendar_returns_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "EXPIRIES_FILE", tmp_path / "absent.json")
    assert ex.days_to_expiry(date(2026, 9, 27), allow_fetch=False) == (None, None)


def test_a_stale_calendar_is_rejected(tmp_path, monkeypatch):
    """Every listed expiry already gone means the cache predates this month."""
    monkeypatch.setattr(ex, "EXPIRIES_FILE", tmp_path / "expiries.json")
    ex.write_json(ex.EXPIRIES_FILE, {"expiries": ["2026-01-15"], "fetched_at": "x"})
    assert ex.days_to_expiry(date(2026, 9, 27), allow_fetch=False) == (None, None)


def test_a_missing_calendar_triggers_one_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "EXPIRIES_FILE", tmp_path / "expiries.json")
    calls = []

    def fake_fetch():
        calls.append(1)
        return CALENDAR

    monkeypatch.setattr(ex, "fetch_expiries", fake_fetch)
    assert ex.days_to_expiry(date(2026, 9, 27), allow_fetch=True) == (date(2026, 10, 15), 18)
    assert len(calls) == 1
    assert ex.EXPIRIES_FILE.exists()  # cached for next time


def test_refresh_cache_survives_a_failed_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "EXPIRIES_FILE", tmp_path / "expiries.json")
    monkeypatch.setattr(
        ex, "fetch_expiries", lambda: (_ for _ in ()).throw(ConnectionError("down"))
    )
    assert ex.refresh_cache() is None  # logged, not raised


# --- the gate --------------------------------------------------------------


def tradeable_signal() -> Signal:
    """Grade A bearish, everything confirming: confidence 85."""
    return generate_signal(1.6, 0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize("days", [3, 4, 18, None])
def test_gate_leaves_a_tradeable_week_alone(days):
    signal = apply_expiry_gate(tradeable_signal(), days)
    assert signal.grade == "A"
    assert signal.confidence == 85
    assert signal.option_type == "put"
    assert signal.skip_reason is None


@pytest.mark.parametrize("days", [0, 1, 2])
def test_gate_forces_a_skip_under_the_floor(days):
    signal = apply_expiry_gate(tradeable_signal(), days)
    assert signal.grade == "skip"
    assert signal.direction == "neutral"
    assert signal.confidence == 0
    assert signal.skip_reason == "expiry"
    # No trade recommendation may survive.
    assert signal.option_type is None
    assert signal.strike_type is None
    assert signal.size_pct is None


def test_the_inventory_read_survives_an_expiry_skip():
    """The data still goes on the record — only the trade is dropped."""
    signal = apply_expiry_gate(tradeable_signal(), 1)
    assert signal.crude_deviation_mb == 1.6
    assert signal.cushing_confirms is True
    assert signal.api_aligns is True


def test_a_deviation_skip_keeps_its_own_reason():
    """The gate must not relabel a skip that the data already caused."""
    signal = apply_expiry_gate(generate_signal(0.4, 0.0, 0.0, 0.0, 0.0), 1)
    assert signal.grade == "skip"
    assert signal.skip_reason == "deviation"


def test_generate_signal_still_knows_nothing_about_expiry():
    """The gate is a wrapper, not a sixth rule. The engine stays pure."""
    import inspect

    assert set(inspect.signature(generate_signal).parameters) == {
        "crude_deviation_mb",
        "gasoline_deviation_mb",
        "distillate_deviation_mb",
        "cushing_mb",
        "api_crude_mb",
    }


def test_reference_week_is_unaffected():
    """Sep 4 2026 sat 30+ days from expiry, so the gate cannot touch it."""
    signal = apply_expiry_gate(generate_signal(1.209, 2.669, 2.787, -0.684, 1.250), 30)
    assert (signal.grade, signal.direction, signal.confidence) == ("B", "bearish", 55)
    assert signal.skip_reason is None


# --- describe -------------------------------------------------------------


def test_describe_flags_the_floor():
    assert "under the 3-day floor" in ex.describe(date(2026, 10, 15), 2)
    assert "18 days left" in ex.describe(date(2026, 10, 15), 18)


def test_describe_says_so_when_unknown():
    text = ex.describe(None, None)
    assert "unknown" in text
    assert "check the option chain" in text
