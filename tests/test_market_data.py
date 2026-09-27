"""Tests for the market-data derived columns and the WTI contract roll.

Offline: yfinance is stubbed. The live fetch is exercised by the manual runbook.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import market_data as md  # noqa: E402


# --- contract ticker generation -------------------------------------------


def test_month_codes_are_the_nymex_set():
    """F G H J K M N Q U V X Z = Jan..Dec. Note I and L are deliberately absent."""
    assert md.MONTH_CODES == "FGHJKMNQUVXZ"
    assert len(md.MONTH_CODES) == 12


@pytest.mark.parametrize(
    ("as_of", "first_three"),
    [
        (date(2026, 9, 27), ["CLU26.NYM", "CLV26.NYM", "CLX26.NYM"]),
        (date(2026, 1, 5), ["CLF26.NYM", "CLG26.NYM", "CLH26.NYM"]),
        # December must roll the year, not just the month code.
        (date(2026, 12, 20), ["CLZ26.NYM", "CLF27.NYM", "CLG27.NYM"]),
    ],
)
def test_contract_tickers_walk_forward_from_the_current_month(as_of, first_three):
    assert md.contract_tickers(as_of)[:3] == first_three


def test_contract_tickers_cross_a_year_boundary_cleanly():
    tickers = md.contract_tickers(date(2026, 11, 1), count=4)
    assert tickers == ["CLX26.NYM", "CLZ26.NYM", "CLF27.NYM", "CLG27.NYM"]


def test_candidate_window_is_wide_enough_for_two_live_contracts():
    """Expired months burn candidates, so the window needs slack."""
    assert md.CONTRACT_CANDIDATES >= 4
    assert len(md.contract_tickers(date(2026, 9, 27))) == md.CONTRACT_CANDIDATES


# --- picking the front two ------------------------------------------------


def _download_stub(frame: pd.DataFrame):
    """Stand in for yf.download, returning a Close-keyed frame."""
    return lambda *a, **k: pd.concat({"Close": frame}, axis=1)


def test_front_two_are_the_nearest_contracts_with_data(monkeypatch):
    """Expired contracts come back empty and must be skipped, in order."""
    index = pd.to_datetime(["2026-09-24", "2026-09-25"])
    frame = pd.DataFrame(
        {
            "CLU26.NYM": [float("nan"), float("nan")],  # expired
            "CLV26.NYM": [float("nan"), float("nan")],  # expired
            "CLX26.NYM": [92.0, 92.41],                 # front
            "CLZ26.NYM": [88.5, 88.71],                 # second
            "CLF27.NYM": [86.0, 86.01],
        },
        index=index,
    )
    monkeypatch.setattr(md.yf, "download", _download_stub(frame))

    result = md.fetch_front_contracts(as_of=date(2026, 9, 27))
    assert list(result.columns) == ["wti_m1_price", "wti_m2_price"]
    assert result["wti_m1_price"].iloc[-1] == 92.41
    assert result["wti_m2_price"].iloc[-1] == 88.71
    assert result.index[-1] == "2026-09-25"


def test_empty_when_fewer_than_two_live_contracts(monkeypatch):
    index = pd.to_datetime(["2026-09-25"])
    frame = pd.DataFrame({"CLU26.NYM": [float("nan")], "CLV26.NYM": [92.41]}, index=index)
    monkeypatch.setattr(md.yf, "download", _download_stub(frame))
    assert md.fetch_front_contracts(as_of=date(2026, 9, 27)).empty


def test_empty_when_yfinance_returns_nothing(monkeypatch):
    monkeypatch.setattr(md.yf, "download", lambda *a, **k: pd.DataFrame())
    assert md.fetch_front_contracts(as_of=date(2026, 9, 27)).empty


# --- the front-month cross-check ------------------------------------------


def test_cross_check_is_quiet_when_m1_tracks_the_continuous(caplog):
    closes = pd.DataFrame({"wti_close": [92.41], "wti_m1_price": [92.41]})
    md._check_front_month(closes)
    assert "check the contract roll" not in caplog.text


def test_cross_check_warns_when_m1_drifts_from_the_continuous(caplog):
    """A misread roll would measure the spread off the wrong pair."""
    import logging

    caplog.set_level(logging.WARNING)
    closes = pd.DataFrame({"wti_close": [92.41], "wti_m1_price": [88.71]})
    md._check_front_month(closes)
    assert "check the contract roll" in caplog.text


def test_cross_check_tolerates_missing_columns():
    md._check_front_month(pd.DataFrame({"wti_close": [92.41]}))  # must not raise


# --- derived columns ------------------------------------------------------


def test_m1m2_spread_sign_convention():
    """Positive is backwardation (tight); negative is contango (oversupplied)."""
    backwardation = md.calculate_derived({"wti_m1_price": 92.41, "wti_m2_price": 88.71})
    assert backwardation["wti_m1m2_spread"] == pytest.approx(3.70, abs=1e-9)

    contango = md.calculate_derived({"wti_m1_price": 88.71, "wti_m2_price": 92.41})
    assert contango["wti_m1m2_spread"] == pytest.approx(-3.70, abs=1e-9)


def test_spread_is_null_without_both_legs():
    assert md.calculate_derived({"wti_m1_price": 92.41})["wti_m1m2_spread"] is None
    assert md.calculate_derived({"wti_m2_price": 88.71})["wti_m1m2_spread"] is None


def test_crack_and_mcx_and_brent_spread():
    row = md.calculate_derived(
        {
            "wti_close": 92.41,
            "brent_close": 97.44,
            "usd_inr_close": 95.802,
            "rbob_close": 3.1716,
            "heating_oil_close": 4.3964,
        }
    )
    assert row["brent_wti_spread"] == pytest.approx(5.03, abs=1e-9)
    # 42 gallons per barrel, 3-2-1 crack.
    expected = (2 * 3.1716 * 42 + 4.3964 * 42 - 3 * 92.41) / 3
    assert row["crack_321"] == pytest.approx(round(expected, 3), abs=1e-9)
    assert row["mcx_close"] == pytest.approx(round(92.41 * 95.802, 2), abs=1e-9)
    assert row["mcx_source"] == "calculated"


def test_derived_columns_survive_a_row_of_nothing():
    row = md.calculate_derived({})
    for field in ("wti_m1m2_spread", "crack_321", "brent_wti_spread", "mcx_close"):
        assert row[field] is None
