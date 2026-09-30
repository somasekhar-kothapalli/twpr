import json

import pytest

from app import telegram_bot as tb
from app.signal_engine import build_signal
from app.utils.common import fmt, now_ist

MARKET = {"atr_20": 4.839, "ovx": 53.74, "wti": 89.7, "cl1_cl2": 2.24, "crack_321": 61.6, "brent_wti": 6.8}
REFERENCE = {
    "release_date": "23-09-2026",
    "crude_consensus_mb": -0.6, "gasoline_consensus_mb": 0.1, "distillate_consensus_mb": -0.6,
    "crude_change_mb": 2.969, "cushing_change_mb": 2.266, "cushing_level_mb": 23.748,
    "gasoline_change_mb": -1.686, "distillate_change_mb": -0.428, "refinery_util_change_pct": -2.8,
    "api_crude_mb": 1.786,
}


def signal(analysis="Crude built more than expected; Cushing confirms.", sigma=1.5, lots=None, market=None, **overrides):
    return build_signal({**REFERENCE, **overrides}, market or MARKET, sigma, 84.0, "yfinance", lots,
                        analyse=lambda p: (analysis, "groq/x" if analysis else "rule_based"),
                        generated_at="23-09-2026 20:02")


def test_regime_1_message_has_everything_needed_to_act():
    text = tb.format_signal(signal(lots={"CRUDEOIL": 2, "CRUDEOILM": 3}))
    assert text.splitlines()[0] == "🔴 TWPR SIGNAL - Regime 1 Bearish: PUT ITM"
    for expected in (
        "Release 23-09-2026 (generated 23-09-2026 20:02 IST)",
        "TLS +2.226 mb | Z +1.48 (sigma 1.500)",
        "Surprises: crude +3.569 | gasoline -1.786 | distillate +0.172",
        "Cushing: +2.266 mb (confirms), level 23.7 mb, x1.39",
        "API crude: +1.786 mb (aligns)",
        "Option: delta 0.80-0.85 (OVX 53.7 above 35, deep ITM) | expiry 15-10-2026 (22d)",
        "Expected WTI move (beta_vol): -2.01 USD = -168 INR on MCX (USD/INR 84.00, yfinance), 0.90 USD per mb",
        "Anchor (0.15-0.30 USD per mb): -0.33 to -0.67 USD = -28 to -56 INR - beta_vol is outside it",
        "MCX limit: futures band 4% = INR 301 (on ~INR 7,535/bbl); the beta_vol move is 2.2% of price = 56% of the band; "
        "the limit widens to 6% then 9%",
        "CRUDEOIL: 2 lots x 100 bbl | INR lost if the option stop is hit, by futures stop:",
        "$0.18: 2,495 | $0.25: 3,465 | $0.35: 4,851",
        "CRUDEOILM: 3 lots x 10 bbl | INR lost if the option stop is hit, by futures stop:",
        "$0.18: 374 | $0.25: 520 | $0.35: 728",
        "IST: print 20:00 | time stop 20:35 | hard exit 22:30 | chop exit 4 min",
        "Checklist:",
        "- Limit orders only on the option chain",
        "Crude built more than expected; Cushing confirms.",
    ):
        assert expected in text, expected
    assert "REPLAY" not in text and len(text) < 4096


def test_bullish_message_and_calm_market_without_deepening():
    text = tb.format_signal(signal(market={**MARKET, "ovx": 30.0, "atr_20": 2.0}, crude_change_mb=-4.0,
                                   gasoline_change_mb=-1.0, distillate_change_mb=-1.0, cushing_change_mb=-1.0,
                                   api_crude_mb=-1.0))
    assert text.splitlines()[0] == "🟢 TWPR SIGNAL - Regime 1 Bullish: CALL ITM"
    assert "Option: delta 0.60-0.70 |" in text and "deep ITM" not in text and "is outside it" not in text
    assert "Anchor (0.15-0.30 USD per mb): +" in text


def test_regime_2_says_targets_are_chart_levels():
    text = tb.format_signal(signal(cushing_change_mb=-1.5, cushing_level_mb=40.0))
    assert text.splitlines()[0] == "🟢 TWPR SIGNAL - Regime 2 Bullish: CALL ITM"
    assert "Cushing: -1.500 mb (contradicts)" in text and "Targets: chart levels" in text and "Expected WTI move" not in text


def test_an_assumed_expiry_is_marked_in_the_message():
    text = tb.format_signal(signal(release_date="23-12-2026", crude_change_mb=12.0))
    assert "expiry 15-01-2027" in text and "ASSUMED - check the chain" in text
    assert "ASSUMED" not in tb.format_signal(signal())          # a calendar month is not flagged


def test_missing_lot_count_says_how_to_set_it():
    assert "Lots: set MCX_CRUDEOIL_LOT_SIZE (or MCX_CRUDEOILM_LOT_SIZE) in .env" in tb.format_signal(signal())


def test_unknown_cushing_reads_unknown_not_a_crash():
    text = tb.format_signal(signal(cushing_change_mb=None, cushing_level_mb=None))
    assert "Cushing: N/A (unknown), level unknown" in text


def test_stand_down_says_no_trade_and_shows_no_trade_details():
    text = tb.format_signal(signal(analysis="", sigma=5.0))
    assert text.splitlines()[0] == "⚪ TWPR - STAND DOWN"
    assert "TLS +2.226 mb, Z +0.45 (sigma 5.000): inside the 1.25 sigma noise band." in text
    assert "No position this week." in text
    for absent in ("Option:", "Checklist", "Expected WTI", "Cushing", "Lots:"):
        assert absent not in text


def test_no_narrative_means_no_empty_trailing_block():
    assert not tb.format_signal(signal(analysis="")).endswith("\n")


def test_replay_is_stamped_and_fits_telegrams_limit():
    text = tb.format_signal(signal(lots={"CRUDEOIL": 2}), replay_days=7)
    assert text.splitlines()[0] == "🔁 REPLAY - data is 7 days old, NOT a live signal"
    assert len(text) < 4096


# ---------------------------------------------------------------------- main

def arm(monkeypatch, tmp_path, content=None):
    path = tmp_path / "signal.json"
    if content is not None:
        path.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(tb, "SIGNAL_FILE", path)
    monkeypatch.setattr(tb, "load_dotenv", lambda *a, **k: None)
    sent, errors = [], []
    monkeypatch.setattr(tb, "send_message", lambda text: sent.append(text) or True)
    monkeypatch.setattr(tb, "send_error", lambda script, msg: errors.append((script, msg)))
    return sent, errors


def fresh_signal():
    return signal(release_date=fmt(now_ist().date()))


def test_main_sends_a_fresh_signal(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, fresh_signal())
    assert tb.main([]) == 0
    assert len(sent) == 1 and sent[0].startswith("\U0001F534 TWPR SIGNAL") and "REPLAY" not in sent[0] and errors == []


def test_main_refuses_a_stale_signal_instead_of_sending_last_weeks_trade(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, signal(release_date="01-01-2026"))
    assert tb.main([]) == 1
    assert sent == [] and errors and "days old" in errors[0][1] and "not sending a stale signal" in errors[0][1]


def test_main_allow_stale_sends_it_stamped_as_a_replay(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, signal(release_date="01-01-2026"))
    assert tb.main(["--allow-stale"]) == 0
    assert sent[0].startswith("\U0001F501 REPLAY") and errors == []


def test_main_missing_file_alerts(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path)
    assert tb.main([]) == 1
    assert sent == [] and "did signal_engine.py run" in errors[0][1]


def test_main_corrupt_file_alerts(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, {"release_date": fmt(now_ist().date())})   # no signal/calculations blocks
    assert tb.main([]) == 1
    assert sent == [] and errors and errors[0][0] == "telegram_bot.py"


def test_main_reports_failure_when_telegram_itself_fails(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, fresh_signal())
    monkeypatch.setattr(tb, "send_message", lambda text: False)
    assert tb.main([]) == 1
