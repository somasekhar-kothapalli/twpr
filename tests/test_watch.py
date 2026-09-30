import json
from datetime import datetime, timedelta

import pytest

from app import watch
from app.utils.common import IST

TRADE = {"release_date": "23-09-2026",
         "signal": {"action": "trade", "regime": 1, "direction": "bearish", "option_type": "PUT", "strike_type": "ITM"},
         "schedule": {"release_ist": "20:00", "time_stop_ist": "20:35", "hard_exit_ist": "22:30",
                      "session_close_ist": "23:30", "chop_exit_min": 4}}
STAND_DOWN = {**TRADE, "signal": {**TRADE["signal"], "action": "stand_down", "regime": None}}


def ist(hh, mm, day=23):
    return datetime(2026, 9, day, hh, mm, tzinfo=IST)


def test_a_trade_gets_four_reminders_in_time_order_with_the_right_times():
    items = watch.reminders(TRADE)
    assert [when for when, _ in items] == [ist(20, 2), ist(20, 35), ist(22, 15), ist(22, 30)]
    texts = [text for _, text in items]
    assert all("PUT ITM (Regime 1 bearish)" in t for t in texts)
    assert "Limit orders only" in texts[0] and "4 minutes after YOUR fill" in texts[0]
    assert "35-MINUTE TIME STOP (20:35 IST)" in texts[1]
    assert "hard exit in 15 minutes (22:30 IST)" in texts[2]
    assert "HARD EXIT NOW" in texts[3] and "MCX closes 23:30 IST" in texts[3]


def test_a_stand_down_has_nothing_to_watch():
    assert watch.reminders(STAND_DOWN) == []


def test_winter_reminders_follow_the_capped_hard_exit():
    winter = {**TRADE, "schedule": {**TRADE["schedule"], "release_ist": "21:00", "time_stop_ist": "21:35",
                                    "hard_exit_ist": "22:55", "session_close_ist": "23:55"}}
    assert [when for when, _ in watch.reminders(winter)][-2:] == [ist(22, 40), ist(22, 55)]


def test_run_sleeps_until_each_reminder_and_sends_it():
    clock = {"now": ist(19, 55)}
    sleeps, sent = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    count = watch.run(watch.reminders(TRADE), now=lambda: clock["now"], sleep=sleep, send=lambda t: sent.append(t) or True)
    assert count == 4 and len(sent) == 4
    assert sleeps == [7 * 60, 33 * 60, 100 * 60, 15 * 60]           # 19:55 -> 20:02 -> 20:35 -> 22:15 -> 22:30


def test_a_reminder_already_far_past_is_skipped_not_sent_late():
    sent = []
    count = watch.run(watch.reminders(TRADE), now=lambda: ist(20, 40), sleep=lambda s: None,
                      send=lambda t: sent.append(t) or True)
    assert count == 2 and len(sent) == 2                             # the 20:02 and 20:35 ones are gone
    assert "hard exit in 15 minutes" in sent[0] and "HARD EXIT NOW" in sent[1]


def test_a_reminder_a_few_seconds_late_is_still_sent():
    sent = []
    watch.run(watch.reminders(TRADE)[:1], now=lambda: ist(20, 2) + timedelta(seconds=30), sleep=lambda s: None,
              send=lambda t: sent.append(t) or True)
    assert len(sent) == 1


def test_a_failed_send_does_not_stop_the_later_reminders():
    outcomes = iter([False, True, True, True])
    count = watch.run(watch.reminders(TRADE), now=lambda: ist(19, 0), sleep=lambda s: None, send=lambda t: next(outcomes))
    assert count == 3


def test_dry_run_lists_without_sleeping_or_sending():
    sent, sleeps = [], []
    count = watch.run(watch.reminders(TRADE), now=lambda: ist(19, 0), sleep=sleeps.append,
                      send=lambda t: sent.append(t), dry_run=True)
    assert count == 4 and sent == [] and sleeps == []


# ------------------------------------------------------------------------------------------- main

def arm(monkeypatch, tmp_path, signal=TRADE):
    path = tmp_path / "signal.json"
    if signal is not None:
        path.write_text(json.dumps(signal), encoding="utf-8")
    monkeypatch.setattr(watch, "SIGNAL_FILE", path)
    monkeypatch.setattr(watch, "load_dotenv", lambda *a, **k: None)
    sent, errors = [], []
    monkeypatch.setattr(watch, "send_message", lambda text: sent.append(text) or True)
    monkeypatch.setattr(watch, "send_error", lambda script, msg: errors.append((script, msg)))
    return sent, errors


def test_main_watches_todays_signal(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path)
    clock = {"now": ist(19, 59)}

    def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)
    assert watch.main([], now=lambda: clock["now"], sleep=sleep) == 0
    assert len(sent) == 4 and errors == []


def test_main_refuses_a_signal_that_is_not_for_today(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path)
    assert watch.main([], now=lambda: ist(19, 0, day=30), sleep=lambda s: None) == 1
    assert sent == [] and "not for today" in errors[0][1] or "is for 23-09-2026, not today" in errors[0][1]


def test_main_allow_stale_replays(monkeypatch, tmp_path):
    sent, _ = arm(monkeypatch, tmp_path)
    assert watch.main(["--allow-stale", "--dry-run"], now=lambda: ist(19, 0, day=30), sleep=lambda s: None) == 0
    assert sent == []


def test_main_stand_down_is_silent_and_ok(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, STAND_DOWN)
    assert watch.main([], now=lambda: ist(19, 0), sleep=lambda s: None) == 0 and sent == [] and errors == []


def test_main_missing_signal_alerts(monkeypatch, tmp_path):
    sent, errors = arm(monkeypatch, tmp_path, None)
    assert watch.main([], now=lambda: ist(19, 0), sleep=lambda s: None) == 1
    assert "did signal_engine.py run" in errors[0][1]
