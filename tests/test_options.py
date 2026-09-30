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
    assert summer == {"release_ist": "20:00", "time_stop_ist": "20:35", "hard_exit_ist": "22:30",
                      "session_close_ist": "23:30", "chop_exit_min": 4}
    winter = options.release_schedule(date(2026, 12, 9))
    # print 21:00 IST; "print + 2.5 h" would be 23:30, but the session runs to 23:55: capped an hour before it
    assert (winter["release_ist"], winter["hard_exit_ist"], winter["session_close_ist"]) == ("21:00", "22:55", "23:55")
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


# ------------------------------------------------------------ MCX holidays (trading holidays page, 2026)

def test_only_days_with_both_sessions_closed_are_holidays_for_the_expiry_count():
    assert options.HOLIDAYS == frozenset({date(2026, 1, 26), date(2026, 4, 3), date(2026, 10, 2), date(2026, 12, 25)})
    assert date(2026, 9, 14) not in options.HOLIDAYS      # Ganesh Chaturthi: the morning is closed, the evening trades


@pytest.mark.parametrize("day,is_open,reason", [
    (date(2026, 9, 23), True, None),                       # an ordinary Wednesday
    (date(2026, 3, 3), True, None),                        # Holi: morning closed, evening OPEN
    (date(2026, 10, 20), True, None),                      # Dassera: same
    (date(2026, 1, 1), False, "New Year Day"),             # the reverse: morning open, evening CLOSED
    (date(2026, 1, 26), False, "Republic Day"),
    (date(2026, 10, 2), False, "Mahatma Gandhi Jayanti"),
    (date(2026, 12, 25), False, "Christmas"),
    (date(2026, 9, 26), False, "weekend"),
])
def test_the_evening_session_is_what_matters_for_an_eia_release(day, is_open, reason):
    assert options.mcx_evening_session(day) == (is_open, reason)


def test_every_listed_holiday_is_a_weekday_and_the_table_matches_the_page():
    assert len(options.MCX_HOLIDAYS_2026) == 16
    assert all(day.weekday() < 5 for day in options.MCX_HOLIDAYS_2026)
    both_closed = [n for n, m, e in options.MCX_HOLIDAYS_2026.values() if not m and not e]
    assert both_closed == ["Republic Day", "Good Friday", "Mahatma Gandhi Jayanti", "Christmas"]


def test_no_2026_holiday_moves_any_option_expiry(monkeypatch):
    """Checked against the calendar: the holidays fall outside every two-business-day lead, so the expiries
    computed from weekends alone are the same as with the holiday list."""
    with_holidays = [options.option_expiry(2026, m) for m in range(1, 13)]
    monkeypatch.setattr(options, "HOLIDAYS", frozenset())
    assert with_holidays == [options.option_expiry(2026, m) for m in range(1, 13)]


@pytest.mark.parametrize("day,close", [
    (date(2026, 9, 23), (23, 30)),    # US daylight saving time: 23:30
    (date(2026, 10, 30), (23, 30)),   # the last Friday before the change
    (date(2026, 11, 2), (23, 55)),    # circular MCX/TRD/550/2026: 23:55 from Monday 2 Nov 2026...
    (date(2026, 12, 9), (23, 55)),
    (date(2027, 3, 12), (23, 55)),    # ...through Friday 12 Mar 2027
    (date(2027, 3, 15), (23, 30)),    # US clocks changed on Sunday 14 Mar: back to 23:30
])
def test_session_close_follows_the_circular(day, close):
    assert options.session_close(day) == close


def test_the_hard_exit_is_never_within_an_hour_of_the_close():
    for day in (date(2026, 9, 23), date(2026, 12, 9), date(2027, 1, 13), date(2027, 3, 10), date(2027, 3, 17)):
        s = options.release_schedule(day)
        hard = int(s["hard_exit_ist"][:2]) * 60 + int(s["hard_exit_ist"][3:])
        close = int(s["session_close_ist"][:2]) * 60 + int(s["session_close_ist"][3:])
        assert close - hard >= options.HARD_EXIT_BEFORE_CLOSE_MIN


# -------------------------------------------------------------- strike guidance (no option chain available)

F, IV, DAYS = 8640.0, 0.54, 22


def test_black76_delta_is_the_textbook_number_at_the_money():
    assert options.black76_delta(F, F, IV, DAYS, "CALL") == pytest.approx(0.5 + 0.0, abs=0.06)
    assert options.black76_delta(F, F, IV, DAYS, "PUT") == pytest.approx(options.black76_delta(F, F, IV, DAYS, "CALL") - 1)
    assert options.black76_delta(F, F * 0.8, IV, DAYS, "CALL") > 0.9 and options.black76_delta(F, F * 1.2, IV, DAYS, "PUT") < -0.9


@pytest.mark.parametrize("option_type", ["CALL", "PUT"])
@pytest.mark.parametrize("delta", [0.60, 0.70, 0.80, 0.85])
def test_the_strike_for_a_delta_really_has_that_delta(option_type, delta):
    strike = options.strike_for_delta(F, IV, DAYS, delta, option_type)
    assert abs(options.black76_delta(F, strike, IV, DAYS, option_type)) == pytest.approx(delta, abs=1e-9)
    assert (strike < F) if option_type == "CALL" else (strike > F)          # ITM: calls below the futures, puts above


def test_strike_guidance_matches_the_hand_calculation_and_rounds_to_50():
    guide = options.strike_guidance(F, 54.0, 22, (0.80, 0.85), "CALL")
    assert guide["strike_at_delta_low"] == 7800 and guide["strike_at_delta_high"] == 7600     # 7,794 and 7,595 unrounded
    assert guide["atm_strike"] == 8650 and guide["futures_level_inr"] == 8640
    assert guide["delta_at_delta_low_strike"] == pytest.approx(0.80, abs=0.01)
    put = options.strike_guidance(F, 54.0, 22, (0.80, 0.85), "PUT")
    assert put["strike_at_delta_low"] > F and put["strike_at_delta_high"] > put["strike_at_delta_low"]
    assert all(v % 50 == 0 for k, v in put.items() if k.startswith("strike_at") or k == "atm_strike")


def test_a_longer_expiry_or_higher_volatility_pushes_the_target_strike_deeper():
    near = options.strike_guidance(F, 54.0, 15, (0.80, 0.85), "CALL")["strike_at_delta_high"]
    far = options.strike_guidance(F, 54.0, 38, (0.80, 0.85), "CALL")["strike_at_delta_high"]
    calm = options.strike_guidance(F, 30.0, 22, (0.80, 0.85), "CALL")["strike_at_delta_high"]
    assert far < near and calm > near                                 # deeper ITM = LOWER call strike
