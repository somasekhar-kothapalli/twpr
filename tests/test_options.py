from datetime import date

import pytest

from app import options


def test_delta_deepens_only_when_ovx_is_strictly_above_35():
    assert options.target_delta(35.0) == (0.60, 0.70, False)
    assert options.target_delta(35.01) == (0.80, 0.85, True)
    assert options.target_delta(20.0)[2] is False


def test_option_expiry_is_two_business_days_before_the_futures_expiry():
    assert options.futures_expiry(2026, 10) == date(2026, 10, 19)      # a Monday
    assert options.option_expiry(2026, 10) == date(2026, 10, 15)       # the confirmed date: Thursday 15 Oct
    assert options.option_expiry(2026, 11) == date(2026, 11, 17)       # futures Thu 19th -> Tue 17th
    assert options.option_expiry(2027, 1) == date(2027, 1, 15)         # futures Tue 19th -> Fri 15th
    assert options.futures_expiry(2026, 9) == date(2026, 9, 18)        # the 19th is a Saturday: Friday
    assert options.option_expiry(2026, 9) == date(2026, 9, 16)


def test_a_holiday_is_skipped_when_counting_back(monkeypatch):
    monkeypatch.setattr(options, "HOLIDAYS", frozenset({date(2026, 10, 16)}))
    assert options.option_expiry(2026, 10) == date(2026, 10, 14)       # Fri 16th no longer counts


@pytest.mark.parametrize("release,expiry,days,rolled", [
    (date(2026, 9, 23), "15-10-2026", 22, False),     # the confirmed October expiry
    (date(2026, 10, 7), "15-10-2026", 8, False),
    (date(2026, 10, 9), "15-10-2026", 6, False),      # 6 days left: still the current month
    (date(2026, 10, 10), "17-11-2026", 38, True),     # exactly 5 days left (a Saturday): roll
    (date(2026, 10, 15), "17-11-2026", 33, True),     # expiry day itself
    (date(2026, 10, 16), "17-11-2026", 32, False),    # October is gone: November is simply the nearest
    (date(2026, 12, 15), "15-01-2027", 31, True),     # December options expired on the 16th... 1 day away: roll
    (date(2026, 12, 24), "15-01-2027", 22, False),    # rolls over the year end
])
def test_expiry_gate(release, expiry, days, rolled):
    assert options.pick_expiry(release) == {"expiry_date": expiry, "days_to_expiry": days, "rolled": rolled}


def test_risk_matches_the_runbook_arithmetic():
    # $0.25 stop at 84 INR = 21 -> x0.65 delta = 13.65 INR per bbl -> x100 bbl = 1,365 per lot (runbook: 1,400)
    risk = options.sizing({"CRUDEOIL": 1}, 84.0, (0.60, 0.70))
    assert risk["delta_used"] == 0.65
    assert risk["contracts"]["CRUDEOIL"] == {"lots": 1, "barrels_per_lot": 100,
                                            "risk_inr_by_futures_stop_usd": {"0.18": 983, "0.25": 1365, "0.35": 1911}}


def test_ten_mini_lots_carry_the_same_risk_as_one_full_lot():
    both = options.sizing({"CRUDEOIL": 1, "CRUDEOILM": 10}, 84.0, (0.60, 0.70))["contracts"]
    assert both["CRUDEOILM"]["barrels_per_lot"] == 10
    assert both["CRUDEOILM"]["risk_inr_by_futures_stop_usd"] == both["CRUDEOIL"]["risk_inr_by_futures_stop_usd"]


def test_only_the_configured_contracts_appear():
    assert list(options.sizing({"CRUDEOILM": 3}, 84.0, (0.80, 0.85))["contracts"]) == ["CRUDEOILM"]


def test_risk_scales_with_lots_and_with_the_rupee():
    two = options.sizing({"CRUDEOIL": 2}, 84.0, (0.60, 0.70))["contracts"]["CRUDEOIL"]["risk_inr_by_futures_stop_usd"]
    assert two == {"0.18": 1966, "0.25": 2730, "0.35": 3822}
    weak = options.sizing({"CRUDEOIL": 1}, 96.0, (0.60, 0.70))["contracts"]["CRUDEOIL"]["risk_inr_by_futures_stop_usd"]["0.25"]
    assert weak == int(round(0.25 * 96.0 * 0.65 * 100))


def test_deeper_delta_risks_more_per_lot():
    def at(delta):
        return options.sizing({"CRUDEOIL": 1}, 84.0, delta)["contracts"]["CRUDEOIL"]["risk_inr_by_futures_stop_usd"]["0.25"]
    assert at((0.80, 0.85)) > at((0.60, 0.70))


def test_release_clock_follows_us_daylight_saving():
    summer = options.release_schedule(date(2026, 9, 23))
    assert summer == {"release_ist": "20:00", "time_stop_ist": "20:35", "hard_exit_ist": "22:30", "chop_exit_min": 4}
    winter = options.release_schedule(date(2026, 12, 9))
    assert (winter["release_ist"], winter["hard_exit_ist"]) == ("21:00", "23:30")
    assert options.release_schedule(date(2026, 10, 28))["release_ist"] == "20:00"     # EDT until 1 Nov 2026...
    assert options.release_schedule(date(2026, 11, 4))["release_ist"] == "21:00"      # ...then EST
