import json

import pytest

from app import telegram_bot as tb
from app.signal_engine import build_signal
from app.utils.common import fmt, now_ist

REFERENCE = {
    "release_date": "30-09-2026",
    "crude_consensus_mb": -1.6, "gasoline_consensus_mb": -1.4, "distillate_consensus_mb": -0.7,
    "crude_change_mb": -0.391, "cushing_change_mb": -0.684, "gasoline_change_mb": 1.269,
    "distillate_change_mb": 2.087, "refinery_util_change_pct": -2.8,
    "api_crude_mb": 1.25, "api_cushing_mb": -0.684,
}


def signal(analysis="Crude built more than expected; Cushing draw contradicts.", **overrides):
    inputs = {**REFERENCE, **overrides}
    return build_signal(inputs, 84.0, "yfinance", analyse=lambda p: (analysis, "groq/x" if analysis else "rule_based"),
                        generated_at="30-09-2026 20:05")


def test_bearish_grade_b_message_has_everything_needed_to_act():
    text = tb.format_signal(signal())
    assert text.splitlines()[0] == "\U0001F534 TWPR SIGNAL - Grade B Bearish"
    for expected in (
        "Release 30-09-2026 (generated 30-09-2026 20:05 IST)",
        "Crude deviation: +1.209 mb (actual -0.391 mb vs consensus -1.600 mb)",
        "Cushing: -0.684 mb (contradicts)",
        "API crude: +1.250 mb (aligns)",
        "Products strongly oppose: gasoline +2.669 mb, distillate +2.787 mb",
        "Trade: PUT 1-OTM | size 1.5% of capital",
        "Confidence: 55",
        "Expected WTI move: -0.8 to -1.5 USD = -67 to -126 INR on MCX (USD/INR 84.00, yfinance)",
        "Crude built more than expected; Cushing draw contradicts.",
    ):
        assert expected in text, expected
    assert "REPLAY" not in text


def test_bullish_grade_a_message():
    text = tb.format_signal(signal(crude_change_mb=-3.5, cushing_change_mb=-1.0, api_crude_mb=-1.0))
    assert text.splitlines()[0] == "\U0001F7E2 TWPR SIGNAL - Grade A Bullish"
    assert "Trade: CALL ATM | size 2.0% of capital" in text
    assert "Cushing: -1.000 mb (confirms)" in text and "Expected WTI move: +1.5 to +3.0 USD" in text


def test_skip_week_says_no_trade_and_shows_no_trade_details():
    text = tb.format_signal(signal(analysis="", crude_change_mb=-1.2))
    assert text.splitlines()[0] == "⚪ TWPR - NO TRADE"
    assert "Crude deviation: +0.400 mb (inside the ±1.0 skip zone)" in text and "No position this week." in text
    for absent in ("Trade:", "Confidence", "Expected WTI", "Cushing"):
        assert absent not in text


def test_missing_optional_data_reads_na_not_a_crash():
    text = tb.format_signal(signal(cushing_change_mb=None, gasoline_change_mb=None))
    assert "Cushing: N/A (unknown)" in text
    assert "Products strongly oppose" not in text     # insufficient data: flag omitted, not asserted


def test_no_narrative_means_no_empty_trailing_block():
    assert not tb.format_signal(signal(analysis="")).endswith("\n")


def test_replay_is_stamped_and_fits_telegrams_limit():
    text = tb.format_signal(signal(), replay_days=7)
    assert text.splitlines()[0] == "\U0001F501 REPLAY - data is 7 days old, NOT a live signal"
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
