"""Tests for eia_parser's source selection and unit conversion.

Offline. The EIA API is the publisher and wins when a key is set; the scrapers
are the fallback that lets a Wednesday run without one.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import eia_parser as ep  # noqa: E402


def _stub(fetch_eia_actuals):
    """A stand-in scraper module, injected through sys.modules."""
    module = types.ModuleType("stub_scraper")
    module.fetch_eia_actuals = fetch_eia_actuals
    return module

SCRAPED = {
    "crude_change_mb": 2.969,
    "cushing_stocks_mb": 2.266,
    "gasoline_change_mb": -1.686,
    "distillate_change_mb": -0.428,
    "refinery_util_pct": None,
}

API_REPORT = {
    "week_ending": "2026-09-18",
    "report_date": "2026-09-27",
    "crude_change_mb": -0.391,
    "cushing_stocks_mb": -0.684,
    "gasoline_change_mb": 2.669,
    "distillate_change_mb": 2.787,
    "refinery_util_pct": 93.1,
    "source": "eia_api",
    "released_at": "x",
}


# --- unit conversion -------------------------------------------------------


def test_thousand_barrels_become_million_barrels():
    """EIA reports thousand barrels; TWPR works in million barrels."""
    rows = [
        {"period": "2026-09-04", "value": 421300},
        {"period": "2026-08-28", "value": 421691},
    ]
    period, change = ep._weekly_change_mb(rows, "WCESTUS1")
    assert period == "2026-09-04"
    assert change == pytest.approx(-0.391, abs=1e-9)


def test_weekly_change_needs_two_weeks():
    with pytest.raises(ValueError, match="need 2 weeks"):
        ep._weekly_change_mb([{"period": "x", "value": 1}], "WCESTUS1")


def test_weekly_change_rejects_a_null_value():
    rows = [{"period": "x", "value": None}, {"period": "y", "value": 1}]
    with pytest.raises(ValueError, match="null value"):
        ep._weekly_change_mb(rows, "WCESTUS1")


# --- source selection ------------------------------------------------------


def test_api_wins_when_a_key_is_set(monkeypatch):
    monkeypatch.setattr(ep, "fetch_eia_report", lambda key, week: API_REPORT)
    result = ep.fetch_actuals("2026-09-18", "a-key")
    assert result["source"] == "eia_api"
    assert result["refinery_util_pct"] == 93.1


def test_scraper_used_when_no_key(monkeypatch):
    monkeypatch.setitem(sys.modules, "tradingeconomics_scraper", _stub(lambda week: SCRAPED))
    result = ep.fetch_actuals("2026-09-18", None)
    assert result is not None
    assert result["source"] == "tradingeconomics.com"
    assert result["week_ending"] == "2026-09-18"
    assert result["crude_change_mb"] == 2.969
    assert result["refinery_util_pct"] is None


def test_api_failure_falls_through_to_a_scraper(monkeypatch):
    def boom(key, week):
        raise ConnectionError("EIA API down")

    monkeypatch.setattr(ep, "fetch_eia_report", boom)
    monkeypatch.setattr(ep, "_from_scraper", lambda name, week: (
        {"week_ending": week, "source": f"{name}-ok", **SCRAPED}
        if name == "tradingeconomics_scraper" else None
    ))
    result = ep.fetch_actuals("2026-09-18", "a-key")
    assert result["source"] == "tradingeconomics_scraper-ok"


def test_second_scraper_tried_when_first_returns_nothing(monkeypatch):
    tried = []

    def scraper(name, week):
        tried.append(name)
        if name == "investing_scraper":
            return {"week_ending": week, "source": "investing.com", **SCRAPED}
        return None

    monkeypatch.setattr(ep, "_from_scraper", scraper)
    result = ep.fetch_actuals("2026-09-18", None)
    assert tried == ["tradingeconomics_scraper", "investing_scraper"]
    assert result["source"] == "investing.com"


def test_none_when_every_source_is_empty(monkeypatch):
    monkeypatch.setattr(ep, "_from_scraper", lambda name, week: None)
    assert ep.fetch_actuals("2026-09-18", None) is None


# --- the scraper glue ------------------------------------------------------


def test_partial_scraper_data_is_discarded(monkeypatch):
    """A missing leg must not become a null in a file the signal engine reads."""
    partial = {**SCRAPED, "cushing_stocks_mb": None}
    monkeypatch.setitem(sys.modules, "tradingeconomics_scraper", _stub(lambda week: partial))
    assert ep._from_scraper("tradingeconomics_scraper", "2026-09-18") is None


def test_a_raising_scraper_is_survived(monkeypatch):
    """A blocked browser or a dead site must not kill the run."""
    def boom(week):
        raise RuntimeError("playwright is not installed in this interpreter")

    monkeypatch.setitem(sys.modules, "investing_scraper", _stub(boom))
    assert ep._from_scraper("investing_scraper", "2026-09-18") is None


def test_a_scraper_without_the_function_is_skipped(monkeypatch):
    monkeypatch.setitem(sys.modules, "tradingeconomics_scraper", types.ModuleType("stub"))
    assert ep._from_scraper("tradingeconomics_scraper", "2026-09-18") is None


def test_refinery_util_may_be_null_but_the_four_legs_may_not():
    """refinery_util_pct is recorded only, so it is not a required field."""
    assert "refinery_util_pct" not in ep.REQUIRED_FIELDS
    assert set(ep.REQUIRED_FIELDS) == {
        "crude_change_mb",
        "cushing_stocks_mb",
        "gasoline_change_mb",
        "distillate_change_mb",
    }
