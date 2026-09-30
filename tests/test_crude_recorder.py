import json

import pytest

from app import crude_recorder as cr

PATH_DOWN = {"pre_print": 92.04, "prices": {"2": 91.76, "5": 91.83, "30": 91.41, "60": 92.65},
             "moves": {"0": 0.06, "2": -0.28, "5": -0.21, "15": 0.13, "30": -0.63, "60": 0.61},
             "high_1h": 92.65, "low_1h": 91.19}


def decision(tls, z, action="stand_down", regime=None):
    return {"tls_mb": tls, "z_tls": z, "action": action, "regime": regime}


def test_a_build_expects_price_down_and_a_draw_up():
    assert cr.expected_sign(2.2) == -1 and cr.expected_sign(-3.0) == 1 and cr.expected_sign(0) is None


def test_aligned_moves_are_positive_when_price_moves_the_way_the_surprise_says():
    rec = cr.build_record("23-09-2026", decision(2.226, 0.38), PATH_DOWN, None, "t")
    assert rec["expected_direction"] == "down"
    assert rec["aligned_moves"]["2"] == 0.28 and rec["aligned_moves"]["60"] == -0.61   # the bounce at 60 minutes
    assert cr.build_record("x", decision(0.0, 0.0), PATH_DOWN, None, "t")["aligned_moves"] is None


def test_the_signal_file_becomes_a_decision_with_the_demeaned_z():
    signal = {"calculations": {"tls_mb": 6.5, "z_tls": 1.4, "z_tls_demeaned": 0.9, "sigma_forecast_mb": 4.6,
                               "crude_surprise_mb": 5.0, "gasoline_surprise_mb": 1.0, "distillate_surprise_mb": 0.5,
                               "cushing_status": "confirms", "api_surprise_mb": 2.0},
              "signal": {"action": "trade", "regime": 1, "direction": "bearish", "reason": None},
              "inputs": {"ovx": 51.9, "atr_20": 4.5}}
    d = cr.decision_from_signal(signal)
    assert (d["action"], d["regime"], d["z_tls_demeaned"], d["ovx"]) == ("trade", 1, 0.9, 51.9)
    assert d["surprises_mb"] == {"crude": 5.0, "gasoline": 1.0, "distillate": 0.5}


def test_a_history_row_is_enough_when_the_signal_is_gone():
    d = cr.decision_from_history({"release_date": "23-09-2026", "crude_surprise_mb": 3.569,
                                  "gasoline_surprise_mb": -1.786, "distillate_surprise_mb": 0.172})
    assert d["tls_mb"] == pytest.approx(3.569 + 0.8 * -1.786 + 0.5 * 0.172, abs=0.01)   # September: w_g 0.80, w_d 0.50


def test_market_snapshot_only_when_the_market_file_is_from_that_week():
    market = {"wti": 94.6, "atr_20": 4.5, "ovx": 51.9, "fetched_at": "30-09-2026 19:00"}
    assert cr.market_snapshot("30-09-2026", market)["ovx"] == 51.9
    assert cr.market_snapshot("30-09-2026", {**market, "fetched_at": "20-09-2026 19:00"}) is None
    assert cr.market_snapshot("30-09-2026", None) is None and cr.market_snapshot("30-09-2026", {}) is None


def test_record_is_idempotent_and_a_poorer_rerun_keeps_the_path():
    data = {"records": {}}
    cr.record("23-09-2026", decision(2.226, 0.38), PATH_DOWN, None, data, "first")
    cr.record("23-09-2026", decision(2.226, 0.38), None, None, data, "second")
    rec = data["records"]["23-09-2026"]
    assert len(data["records"]) == 1 and rec["path"]["pre_print"] == 92.04 and rec["recorded_at"] == "second"


def test_seed_from_the_journal_copies_logged_paths():
    journal_data = {"paths": {"23-09-2026": {"path": PATH_DOWN, "logged_at": "30-09-2026 12:59",
                                             "signal": {"regime": None, "direction": "neutral", "tls_mb": 2.226,
                                                        "z_tls": 0.38}},
                              "16-09-2026": {"path": PATH_DOWN, "signal": None}}}
    data = {"records": {}}
    assert cr.seed_from_journal(data, journal_data) == 1          # a path with no decision snapshot is skipped
    assert data["records"]["23-09-2026"]["decision"]["action"] == "stand_down"


def weeks():
    """Six weeks where price follows the surprise (aligned moves positive) plus one against it."""
    data = {"records": {}}
    rows = [("01-07-2026", -8.0, -1.9, 0.9), ("08-07-2026", 6.0, 1.4, 0.7), ("15-07-2026", -1.0, -0.2, 0.1),
            ("22-07-2026", 2.0, 0.4, 0.2), ("29-07-2026", -7.0, -1.6, 0.6), ("05-08-2026", 7.5, 1.7, 0.8),
            ("12-08-2026", 3.0, 0.6, 0.3)]
    for date, tls, z, up in rows:
        sign = -1 if tls > 0 else 1
        move = up * sign * (-1 if date == "12-08-2026" else 1)   # the last week goes the wrong way
        path = {"moves": {"2": move, "5": move, "15": move, "30": move, "60": move}}
        cr.record(date, decision(tls, z, "trade" if abs(z) >= 1.25 else "stand_down"), path, None, data, "t")
    return data


def test_show_reports_hit_rate_and_slope_for_all_weeks_and_for_gated_weeks():
    lines = cr.show(weeks())
    text = "\n".join(lines)
    assert "-- all weeks" in text and "-- weeks with |Z| >= 1.25" in text
    all_stats = cr._stats(weeks()["records"], "5")
    assert all_stats["n"] == 7 and all_stats["hit_rate"] == pytest.approx(6 / 7)
    gated = cr._stats(weeks()["records"], "5", cr.GATE_Z)
    assert gated["n"] == 4 and gated["hit_rate"] == 1.0 and gated["slope"] > 0.09
    assert cr.show({"records": {}}) == ["nothing recorded yet"]


def test_slope_uses_the_size_of_the_surprise_in_mb():
    data = {"records": {}}
    cr.record("01-07-2026", decision(-10.0, -2.0), {"moves": {"5": 2.0}}, None, data, "t")   # 10 mb draw, +2.0 USD
    assert cr._stats(data["records"], "5")["slope"] == pytest.approx(0.2)


def patch_files(monkeypatch, tmp_path, signal=None, market=None):
    monkeypatch.setattr(cr, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cr, "CRUDE_RECORD_FILE", tmp_path / "rec.json")
    monkeypatch.setattr(cr, "SIGNAL_FILE", tmp_path / "signal.json")
    monkeypatch.setattr(cr, "MARKET_FILE", tmp_path / "market.json")
    monkeypatch.setattr(cr, "SURPRISE_HISTORY_FILE", tmp_path / "history.json")
    if signal is not None:
        (tmp_path / "signal.json").write_text(json.dumps(signal), encoding="utf-8")
    if market is not None:
        (tmp_path / "market.json").write_text(json.dumps(market), encoding="utf-8")
    alerts = []
    monkeypatch.setattr(cr, "send_exception", lambda script, exc: alerts.append(script))
    return alerts


SIGNAL = {"release_date": "23-09-2026",
          "calculations": {"tls_mb": 2.226, "z_tls": 0.38, "z_tls_demeaned": -0.02, "sigma_forecast_mb": 5.8,
                           "crude_surprise_mb": 3.569, "gasoline_surprise_mb": -1.786, "distillate_surprise_mb": 0.172},
          "signal": {"action": "stand_down", "regime": None, "direction": "neutral", "reason": "z_below_gate"},
          "inputs": {"ovx": 51.9, "atr_20": 4.5}}


def fake_bars(date):
    return "bars"


def test_main_records_the_week_in_the_signal_file(monkeypatch, tmp_path):
    alerts = patch_files(monkeypatch, tmp_path, SIGNAL)
    monkeypatch.setattr(cr.journal, "path_from_bars", lambda bars, date: PATH_DOWN)
    assert cr.main(["--date", "23-09-2026"], fetch=fake_bars) == 0 and alerts == []
    saved = cr.load(tmp_path / "rec.json")["records"]["23-09-2026"]
    assert saved["decision"]["action"] == "stand_down" and saved["aligned_moves"]["2"] == 0.28


def test_main_with_an_old_signal_and_no_date_finds_nothing_new_and_is_not_a_failure(monkeypatch, tmp_path):
    alerts = patch_files(monkeypatch, tmp_path, SIGNAL)
    monkeypatch.setattr(cr.journal, "path_from_bars", lambda bars, date: 1 / 0)   # must not be reached
    assert cr.main([], fetch=fake_bars) == 0 and alerts == []
    assert not (tmp_path / "rec.json").exists()


def test_main_alerts_when_the_price_bars_are_missing(monkeypatch, tmp_path):
    alerts = patch_files(monkeypatch, tmp_path, SIGNAL)

    def gone(date):
        raise RuntimeError("no 1-minute WTI bars")
    assert cr.main(["--date", "23-09-2026"], fetch=gone) == 1 and alerts == ["crude_recorder.py"]


def test_main_without_a_signal_or_a_date_is_an_error(monkeypatch, tmp_path):
    alerts = patch_files(monkeypatch, tmp_path)
    assert cr.main([], fetch=fake_bars) == 1 and alerts == ["crude_recorder.py"]
