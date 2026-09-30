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
          "cl1_cl2": 2.24, "crack_321": 30.0, "brent_wti": 6.4, "dxy": 101.6, "overnight_rally_usd": 0.6,
          "fetched_at": "30-09-2026 10:00"}


def test_main_writes_the_market_file(monkeypatch):
    alerts, writes = arm(monkeypatch, MARKET)
    assert md.main([]) == 0
    assert writes == [(md.MARKET_FILE, MARKET)] and alerts == []


def test_main_failure_alerts_and_writes_nothing(monkeypatch):
    boom = RuntimeError("no data for ^OVX")
    alerts, writes = arm(monkeypatch, error=boom)
    assert md.main([]) == 1
    assert alerts == [("market_data.py", boom)] and writes == []


def test_a_failing_scorecard_input_becomes_null_not_a_failure():
    def broken():
        raise RuntimeError("down")
    assert md.optional("DXY", broken) is None
    assert md.optional("DXY", lambda: 101.6) == 101.6


# ------------------------------------------------------------ overnight rally (Regime 3)

from datetime import datetime, timedelta, timezone   # noqa: E402

NY = md.NEW_YORK


def ny(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def test_last_api_time_is_the_most_recent_tuesday_1630_new_york():
    wednesday_morning = ny(2026, 9, 30, 10, 0)
    assert md.last_api_time(wednesday_morning) == ny(2026, 9, 29, 16, 30)
    assert md.last_api_time(ny(2026, 9, 29, 16, 30)) == ny(2026, 9, 29, 16, 30)         # exactly then
    assert md.last_api_time(ny(2026, 9, 29, 15, 0)) == ny(2026, 9, 22, 16, 30)          # Tuesday, before the print
    assert md.last_api_time(ny(2026, 10, 2, 9, 0)) == ny(2026, 9, 29, 16, 30)           # Friday
    assert md.last_api_time(wednesday_morning.astimezone(timezone.utc)) == ny(2026, 9, 29, 16, 30)   # UTC in, NY out


def test_price_at_uses_the_open_of_the_last_bar_and_refuses_a_gap():
    bars = [(ny(2026, 9, 29, 16, 25), 88.0), (ny(2026, 9, 29, 16, 30), 88.2), (ny(2026, 9, 29, 16, 35), 88.4)]
    assert md.price_at(bars, ny(2026, 9, 29, 16, 30)) == 88.2
    assert md.price_at(bars, ny(2026, 9, 29, 16, 32)) == 88.2
    assert md.price_at(bars, ny(2026, 9, 29, 16, 20)) is None                            # nothing yet
    assert md.price_at(bars, ny(2026, 9, 29, 17, 30)) is None                            # last bar is 55 min old


def make_bars():
    """5-minute opens: 88.00 at Tue 16:30 ET, drifting to 89.60 by Wed 09:55, 90.50 just after the print."""
    bars, moment = [], ny(2026, 9, 29, 16, 30)
    while moment <= ny(2026, 9, 30, 11, 0):
        if not (moment.hour == 17):                                                      # the daily break
            frac = (moment - ny(2026, 9, 29, 16, 30)) / timedelta(hours=17)
            price = 88.0 + 1.6 * min(frac, 1.0) if moment < ny(2026, 9, 30, 10, 30) else 90.5
            bars.append((moment, price))
        moment += timedelta(minutes=5)
    return bars


def test_overnight_rally_is_measured_up_to_the_print_and_never_past_it():
    bars = make_bars()
    before = md.overnight_rally(bars, ny(2026, 9, 30, 10, 0))
    assert before["api_price"] == 88.0 and 1.4 < before["rally_usd"] < 1.7
    after = md.overnight_rally(bars, ny(2026, 9, 30, 11, 0))                             # run after the print
    assert after["pre_print_price"] < 90.0 and after["rally_usd"] < 1.7                  # the post-print jump is excluded


def test_overnight_rally_is_none_when_either_price_is_missing():
    assert md.overnight_rally([], ny(2026, 9, 30, 10, 0)) is None
    only_later = [(ny(2026, 9, 30, 9, 0), 89.0)]
    assert md.overnight_rally(only_later, ny(2026, 9, 30, 10, 0)) is None                # no price at the API time


# ---------------------------------------------------------------------- replay (--date)

def test_replay_moment_is_one_minute_before_the_print_new_york_time():
    assert md.replay_moment("23-09-2026") == datetime(2026, 9, 23, 10, 29, tzinfo=NY)
    with pytest.raises(ValueError):
        md.replay_moment("2026-09-23")


def test_main_date_replays_as_of_that_release(monkeypatch):
    seen = []
    monkeypatch.setattr(md, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(md, "write_json", lambda path, payload: None)
    monkeypatch.setattr(md, "fetch_market", lambda **kw: seen.append(kw) or MARKET)
    assert md.main(["--date", "23-09-2026"]) == 0 and md.main([]) == 0
    assert seen == [{"asof": datetime(2026, 9, 23, 10, 29, tzinfo=NY)}, {"asof": None}]


def test_a_bad_date_is_an_argument_error_not_a_silent_live_run(monkeypatch):
    monkeypatch.setattr(md, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        md.main(["--date", "yesterday"])


def test_usd_inr_trend_uses_the_last_five_sessions():
    from app import currency
    closes = [95.0, 95.1, 95.2, 95.3, 95.4, 95.5, 96.0]                   # 7 closes: the trend spans the last 6
    assert currency.trend_pct(closes[-6:]) == pytest.approx((96.0 - 95.1) / 95.1 * 100, abs=1e-3)


def test_a_missing_contract_probe_is_quiet(caplog):
    import logging
    with caplog.at_level(logging.INFO):
        assert md.optional("CLV26.NYM", lambda: 1 / 0, quiet=True) is None
        assert caplog.text == ""
        assert md.optional("DXY", lambda: 1 / 0) is None
    assert "DXY unavailable" in caplog.text
