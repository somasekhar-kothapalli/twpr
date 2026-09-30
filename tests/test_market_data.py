from datetime import date

import pytest

from app import market_data as md


def test_true_range_uses_the_gap_from_the_previous_close():
    bars = [(10, 9, 9.5), (12, 11, 11.5)]          # gap up: |12 - 9.5| beats high-low
    assert md.true_ranges(bars) == [2.5]


def test_atr_is_the_mean_of_the_last_n_true_ranges():
    bars = [(10, 9, 9.5)] + [(11, 10, 10.5)] * 3   # first TR 1.5 (gap), then 1.0, 1.0
    assert md.atr(bars, days=2) == 1.0
    assert md.atr(bars, days=3) == pytest.approx((1.5 + 1 + 1) / 3)


def test_atr_needs_enough_bars():
    with pytest.raises(ValueError, match="need 21 daily bars"):
        md.atr([(1, 1, 1)] * 5)


def test_crack_321_converts_gallons_to_barrels():
    # RBOB $3.00/gal, HO $4.00/gal, WTI $90: (2*126 + 168 - 270) / 3 = 50
    assert md.crack_321(90, 3.0, 4.0) == pytest.approx(50.0)


def test_contract_symbols_roll_over_the_year():
    assert md.contract_symbols(date(2026, 9, 30), 4) == ["CLV26.NYM", "CLX26.NYM", "CLZ26.NYM", "CLF27.NYM"]
    assert md.contract_symbols(date(2026, 12, 15), 2) == ["CLF27.NYM", "CLG27.NYM"]


def test_front_second_finds_the_contract_trading_at_the_continuous_price():
    closes = {"CLX26.NYM": 89.63, "CLZ26.NYM": 87.39, "CLF27.NYM": 85.76}
    assert md.front_second(89.63, closes) == (89.63, 87.39)
    assert md.front_second(89.72, closes) == (89.63, 87.39)       # quote drifted between fetches
    assert md.front_second(89.63, {"CLV26.NYM": None, **closes}) == (89.63, 87.39)   # expired contract


def test_front_second_refuses_to_guess():
    assert md.front_second(95.0,{"CLX26.NYM": 89.63, "CLZ26.NYM": 87.39}) is None
    assert md.front_second(89.63, {"CLX26.NYM": 89.63, "CLZ26.NYM": None}) is None


def arm(monkeypatch, market=None, error=None):
    alerts, writes = [], []
    monkeypatch.setattr(md, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(md, "send_exception", lambda script, exc: alerts.append((script, exc)))
    monkeypatch.setattr(md, "write_json", lambda path, payload: writes.append((path, payload)))

    def fetch(*args, **kwargs):
        if error:
            raise error
        return market
    monkeypatch.setattr(md, "fetch_market", fetch)
    return alerts, writes


MARKET = {"as_of": "29-09-2026", "wti": 89.63, "atr_20": 2.5, "ovx": 54.9,
          "cl1_cl2": 2.24, "crack_321": 30.0, "brent_wti": 6.4, "dxy": 101.6, "fetched_at": "30-09-2026 10:00"}


def test_main_writes_the_market_file(monkeypatch):
    alerts, writes = arm(monkeypatch, MARKET)
    assert md.main() == 0
    assert writes == [(md.MARKET_FILE, MARKET)] and alerts == []


def test_main_failure_alerts_and_writes_nothing(monkeypatch):
    boom = RuntimeError("no data for ^OVX")
    alerts, writes = arm(monkeypatch, error=boom)
    assert md.main() == 1
    assert alerts == [("market_data.py", boom)] and writes == []


def test_a_failing_scorecard_input_becomes_null_not_a_failure():
    def broken():
        raise RuntimeError("down")
    assert md.optional("DXY", broken) is None
    assert md.optional("DXY", lambda: 101.6) == 101.6
