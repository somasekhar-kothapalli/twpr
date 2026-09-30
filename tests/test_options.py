from datetime import date

import pytest

from app import options


def test_delta_deepens_only_when_ovx_is_strictly_above_35():
    assert options.target_delta(35.0) == (0.60, 0.70, False)
    assert options.target_delta(35.01) == (0.80, 0.85, True)
    assert options.target_delta(20.0)[2] is False


# every 2026 futures expiry, straight from MCX's launch calendar, and the option expiry two business days earlier
CALENDAR_2026 = [
    (1, date(2026, 1, 16), date(2026, 1, 14)), (2, date(2026, 2, 19), date(2026, 2, 17)),
    (3, date(2026, 3, 19), date(2026, 3, 17)), (4, date(2026, 4, 20), date(2026, 4, 16)),
    (5, date(2026, 5, 18), date(2026, 5, 14)), (6, date(2026, 6, 18), date(2026, 6, 16)),
    (7, date(2026, 7, 20), date(2026, 7, 16)), (8, date(2026, 8, 19), date(2026, 8, 17)),
    (9, date(2026, 9, 21), date(2026, 9, 17)), (10, date(2026, 10, 19), date(2026, 10, 15)),
    (11, date(2026, 11, 19), date(2026, 11, 17)), (12, date(2026, 12, 18), date(2026, 12, 16)),
]


@pytest.mark.parametrize("month,futures,option", CALENDAR_2026)
def test_2026_expiries_follow_the_mcx_calendar(month, futures, option):
    assert options.futures_expiry(2026, month) == futures and options.expiry_is_known(2026, month)
    assert options.option_expiry(2026, month) == option
    assert (futures - option).days >= 2 and futures.weekday() < 5


def test_the_expiry_day_is_not_always_the_19th():
    """The old fixed-19th rule was wrong for a third of 2026: e.g. September expires Mon 21st, July Mon 20th."""
    assert options.futures_expiry(2026, 9) == date(2026, 9, 21)     # the 19th is a Saturday, MCX moved it FORWARD
    assert options.futures_expiry(2026, 7) == date(2026, 7, 20)     # Sunday 19th -> Monday, not Friday
    assert options.futures_expiry(2026, 1) == date(2026, 1, 16)     # earlier than the 19th
    assert options.option_expiry(2026, 10) == date(2026, 10, 15)    # the date you confirmed


def test_a_month_outside_the_calendar_is_a_flagged_guess():
    assert not options.expiry_is_known(2027, 1)
    assert options.futures_expiry(2027, 1) == date(2027, 1, 19)     # a Tuesday
    assert options.option_expiry(2027, 1) == date(2027, 1, 15)      # two business days earlier (a guess)


def test_a_holiday_is_skipped_when_counting_back(monkeypatch):
    monkeypatch.setattr(options, "HOLIDAYS", frozenset({date(2026, 10, 16)}))
    assert options.option_expiry(2026, 10) == date(2026, 10, 14)       # Fri 16th no longer counts


@pytest.mark.parametrize("release,expiry,days,rolled,source", [
    (date(2026, 9, 14), "15-10-2026", 31, True, "mcx_calendar"),    # Sep options expire the 17th: 3 days: roll
    (date(2026, 9, 11), "17-09-2026", 6, False, "mcx_calendar"),    # 6 days left: stay
    (date(2026, 9, 23), "15-10-2026", 22, False, "mcx_calendar"),   # the confirmed October expiry
    (date(2026, 10, 9), "15-10-2026", 6, False, "mcx_calendar"),    # 6 days left: still the current month
    (date(2026, 10, 10), "17-11-2026", 38, True, "mcx_calendar"),   # exactly 5 days left (a Saturday): roll
    (date(2026, 10, 15), "17-11-2026", 33, True, "mcx_calendar"),   # expiry day itself
    (date(2026, 10, 16), "17-11-2026", 32, False, "mcx_calendar"),  # October is gone: November is the nearest
    (date(2026, 12, 15), "15-01-2027", 31, True, "assumed_19th"),   # Dec expires the 16th; January 2027 is a guess
    (date(2026, 12, 24), "15-01-2027", 22, False, "assumed_19th"),
])
def test_expiry_gate(release, expiry, days, rolled, source):
    assert options.pick_expiry(release) == {"expiry_date": expiry, "days_to_expiry": days, "rolled": rolled,
                                            "expiry_source": source}


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


def test_band_context_measures_the_move_against_the_four_percent_limit():
    band = options.band_context(-168, 7534.8)                       # WTI 89.7 x 84 INR
    assert band["band_pct"] == 4.0 and band["band_inr"] == 301 and band["futures_price_inr"] == 7535
    assert band["move_pct_of_price"] == 2.23 and band["move_share_of_band"] == 0.56 and band["near_band"] is False


def test_a_move_near_the_band_is_flagged_and_direction_does_not_matter():
    up, down = options.band_context(260, 7535), options.band_context(-260, 7535)
    assert up == down and up["near_band"] is True                 # 260 / 301 = 86% of the band
    assert options.band_context(227, 7535)["near_band"] is True    # 75.3% of 301.4
    assert options.band_context(226, 7535)["near_band"] is False   # 74.98%
