from datetime import date

import pytest

from app import options


def test_delta_deepens_only_when_ovx_is_strictly_above_35():
    assert options.target_delta(35.0) == (0.60, 0.70, False)
    assert options.target_delta(35.01) == (0.80, 0.85, True)
    assert options.target_delta(20.0)[2] is False


@pytest.mark.parametrize("release,expiry,days,rolled", [
    (date(2026, 9, 23), "19-10-2026", 26, False),     # 23-09 is past this month's 19th: October's expiry
    (date(2026, 10, 7), "19-10-2026", 12, False),
    (date(2026, 10, 13), "19-10-2026", 6, False),     # 6 days left: still current month
    (date(2026, 10, 14), "19-11-2026", 36, True),     # exactly 5 days left: roll
    (date(2026, 10, 19), "19-11-2026", 31, True),     # expiry day itself
    (date(2026, 12, 15), "19-01-2027", 35, True),     # rolls over the year end
    (date(2026, 12, 24), "19-01-2027", 26, False),
])
def test_expiry_gate(release, expiry, days, rolled):
    assert options.pick_expiry(release) == {"expiry_date": expiry, "days_to_expiry": days, "rolled": rolled}


def test_lots_match_the_runbook_worked_example():
    # $0.25 stop at 84 INR = 21 -> x0.65 delta = 13.65 -> 1,365/lot; 10,000 risk -> 7 lots
    assert options.lots_for_stop(1_000_000, 0.25, 84.0, 0.65) == 7
    assert options.lots_for_stop(1_000_000, 0.25, 84.0, 0.65, max_lots=1) == 1
    assert options.lots_for_stop(1_000_000, 0.25, 84.0, 0.65, max_lots=50) == 7
    assert options.lots_for_stop(5_000, 0.35, 84.0, 0.85) == 0      # account too small: zero lots, not a forced 1


def test_wider_stops_mean_fewer_lots():
    lots = options.sizing(1_000_000, 84.0, (0.60, 0.70))["lots_by_futures_stop_usd"]
    assert lots["0.18"] >= lots["0.25"] >= lots["0.35"] and lots["0.35"] > 0


def test_release_clock_follows_us_daylight_saving():
    summer = options.release_schedule(date(2026, 9, 23))
    assert summer == {"release_ist": "20:00", "time_stop_ist": "20:35", "hard_exit_ist": "22:30", "chop_exit_min": 4}
    winter = options.release_schedule(date(2026, 12, 9))
    assert (winter["release_ist"], winter["hard_exit_ist"]) == ("21:00", "23:30")
    assert options.release_schedule(date(2026, 10, 28))["release_ist"] == "20:00"     # EDT until 1 Nov 2026...
    assert options.release_schedule(date(2026, 11, 4))["release_ist"] == "21:00"      # ...then EST
