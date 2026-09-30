import json
from datetime import datetime, timedelta

import pytest

from app import journal as jr

NY = jr.NEW_YORK
DAY = "23-09-2026"


def minute_bars(pre=90.0, moves=None):
    """1-minute opens from 10:20 to 12:00 ET: `pre` until the print, then pre + moves[minute after print]."""
    moves = moves or {}
    start = datetime(2026, 9, 23, 10, 20, tzinfo=NY)
    bars = []
    for i in range(100):
        moment = start + timedelta(minutes=i)
        after = (moment - jr.print_time(DAY)).total_seconds() / 60
        reached = [k for k in moves if k <= after]                       # a step function: the last move reached so far
        bars.append((moment, pre if after < 0 or not reached else pre + moves[max(reached)]))
    return bars


# --------------------------------------------------------------------------------------- P&L

def test_pnl_on_a_winning_crude_lot_with_estimated_charges():
    r = jr.pnl(1300.0, 1330.0, 1, 100)
    assert r["gross_pnl_inr"] == 3000.0
    assert r["charges_inr"] == pytest.approx(1330 * 100 * 0.0005 + 40)          # CTT on the sold premium + 2 x Rs 20
    assert r["net_pnl_inr"] == pytest.approx(3000 - r["charges_inr"])
    assert r["return_pct"] == pytest.approx(2.31, abs=0.01) and r["capital_deployed_inr"] == 130_000


def test_ten_mini_lots_and_one_crude_lot_have_the_same_gross():
    assert jr.pnl(1300, 1330, 10, 10)["gross_pnl_inr"] == jr.pnl(1300, 1330, 1, 100)["gross_pnl_inr"]


def test_a_loss_is_negative_and_the_charges_still_apply():
    r = jr.pnl(1300.0, 1290.0, 2, 10)
    assert r["gross_pnl_inr"] == -200.0 and r["net_pnl_inr"] < -200.0


# ------------------------------------------------------------------------------------- record

def fill(journal, **overrides):
    args = dict(release_date=DAY, contract="CRUDEOILM", option_type="PUT", strike=9800, lots=3, entry_premium=1310.0,
                exit_premium=1342.0, exit_reason="time_stop", intended_premium=1305.0, logged_at="23-09-2026 21:10")
    args.update(overrides)
    return jr.record_fill(journal, **args)


def test_a_fill_records_pnl_and_slippage_against_the_intended_price():
    journal = {}
    trade = fill(journal)
    assert journal["trades"] == [trade]
    assert trade["gross_pnl_inr"] == (1342 - 1310) * 3 * 10 and trade["slippage_inr"] == (1310 - 1305) * 3 * 10
    assert fill({}, intended_premium=None)["slippage_inr"] is None


@pytest.mark.parametrize("bad", [dict(contract="NATURALGAS"), dict(option_type="STRADDLE"), dict(exit_reason="panic"),
                                 dict(lots=0), dict(entry_premium=0), dict(exit_premium=-1)])
def test_bad_fills_are_refused(bad):
    with pytest.raises(ValueError):
        fill({}, **bad)


def test_the_signal_snapshot_only_attaches_for_the_same_release(tmp_path):
    path = tmp_path / "signal.json"
    path.write_text(json.dumps({"release_date": DAY, "signal": {"regime": 1, "direction": "bearish", "option_type": "PUT"},
                                "calculations": {"tls_mb": 8.1, "z_tls": 1.4}, "expected_move": {"wti_usd": -2.0}}),
                    encoding="utf-8")
    assert jr.signal_snapshot(DAY, path) == {"regime": 1, "direction": "bearish", "option_type": "PUT", "tls_mb": 8.1,
                                             "z_tls": 1.4, "expected_wti_usd": -2.0}
    assert jr.signal_snapshot("30-09-2026", path) is None and jr.signal_snapshot(DAY, tmp_path / "missing.json") is None


# ---------------------------------------------------------------------------------- price path

def test_the_path_is_measured_from_the_price_just_before_the_print():
    path = jr.path_from_bars(minute_bars(90.0, {0: 0.1, 2: 0.5, 5: 1.2, 10: 1.5, 30: 1.0}), DAY)
    assert path["pre_print"] == 90.0
    assert path["moves"]["0"] == pytest.approx(0.1) and path["moves"]["2"] == pytest.approx(0.5)
    assert path["moves"]["5"] == pytest.approx(1.2) and path["moves"]["30"] == pytest.approx(1.0)
    assert path["high_1h"] > 91.0 and path["low_1h"] <= 90.2


def test_the_bar_that_opens_at_the_print_is_not_the_before_price():
    bars = minute_bars(90.0, {0: 3.0})                    # the 10:30 bar already carries the reaction
    assert jr.path_from_bars(bars, DAY)["pre_print"] == 90.0


def test_no_bars_before_the_print_means_no_path():
    late = [b for b in minute_bars() if b[0] >= jr.print_time(DAY)]
    assert jr.path_from_bars(late, DAY) is None


def test_the_print_is_10_30_new_york_time_whatever_the_season():
    assert jr.print_time("23-09-2026") == datetime(2026, 9, 23, 10, 30, tzinfo=NY)
    assert jr.print_time("09-12-2026").utcoffset() == timedelta(hours=-5)      # EST in December


# ------------------------------------------------------------------------------------- summary

def test_the_summary_reports_trades_slippage_and_the_move_in_the_signals_direction():
    journal = {"trades": [], "paths": {}}
    fill(journal)
    fill(journal, exit_premium=1290.0, intended_premium=None)
    bear = jr.path_from_bars(minute_bars(90.0, {0: -0.2, 2: -0.6, 5: -1.0}), DAY)
    journal["paths"][DAY] = {"path": bear, "signal": {"direction": "bearish"}}
    text = "\n".join(jr.summarise(journal))
    assert "Trades: 2 | wins 1 (50%)" in text
    assert "average +150 over 1 fills" in text                                   # (1310-1305) x 3 x 10
    assert "+ 2 min: average +0.60" in text and "+ 5 min: average +1.00" in text  # a bearish signal: a FALL is positive


def test_an_empty_journal_says_so():
    assert jr.summarise({"trades": [], "paths": {}}) == ["Trades: none logged yet"]


def test_paths_without_a_traded_signal_are_counted_not_averaged():
    journal = {"trades": [], "paths": {DAY: {"path": jr.path_from_bars(minute_bars(), DAY), "signal": None}}}
    assert "1 price paths logged, none for a traded signal yet" in "\n".join(jr.summarise(journal))


# ---------------------------------------------------------------------------------------- main

def arm(monkeypatch, tmp_path):
    monkeypatch.setattr(jr, "JOURNAL_FILE", tmp_path / "journal.json")
    monkeypatch.setattr(jr, "SIGNAL_FILE", tmp_path / "signal.json")
    monkeypatch.setattr(jr, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(jr, "load", lambda path=None: json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
                        if (tmp_path / "journal.json").exists() else {"trades": [], "paths": {}})
    return tmp_path / "journal.json"


def test_main_fill_writes_the_journal(monkeypatch, tmp_path):
    file = arm(monkeypatch, tmp_path)
    argv = ["fill", "--date", DAY, "--contract", "CRUDEOILM", "--option", "PUT", "--strike", "9800", "--lots", "3",
            "--entry-premium", "1310", "--exit-premium", "1342", "--exit-reason", "time_stop", "--intended-premium", "1305"]
    assert jr.main(argv) == 0
    saved = json.loads(file.read_text(encoding="utf-8"))
    assert len(saved["trades"]) == 1 and saved["trades"][0]["net_pnl_inr"] > 0
    assert jr.main(argv) == 0 and len(json.loads(file.read_text(encoding="utf-8"))["trades"]) == 2   # appends


def test_main_fill_rejects_a_bad_contract_with_an_argument_error(monkeypatch, tmp_path):
    arm(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        jr.main(["fill", "--date", DAY, "--contract", "GOLD", "--option", "PUT", "--strike", "1", "--lots", "1",
                 "--entry-premium", "1", "--exit-premium", "1", "--exit-reason", "manual"])


def test_main_path_stores_the_price_path(monkeypatch, tmp_path):
    file = arm(monkeypatch, tmp_path)
    assert jr.main(["path", "--date", DAY], fetch=lambda d: minute_bars(90.0, {2: 0.5})) == 0
    assert json.loads(file.read_text(encoding="utf-8"))["paths"][DAY]["path"]["pre_print"] == 90.0


def test_main_path_reports_when_yahoo_no_longer_has_the_bars(monkeypatch, tmp_path):
    arm(monkeypatch, tmp_path)

    def gone(d):
        raise RuntimeError("no 1-minute WTI bars")
    assert jr.main(["path", "--date", DAY], fetch=gone) == 1 and not (tmp_path / "journal.json").exists()


def test_main_show_works_on_an_empty_journal(monkeypatch, tmp_path):
    arm(monkeypatch, tmp_path)
    assert jr.main(["show"]) == 0
