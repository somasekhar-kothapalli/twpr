"""MCX options mechanics from docs/WPSR_MCX_OPTIONS_RUNBOOK.md. Pure: no I/O, no clock.

Strike (ITM delta), expiry gate, the loss per lot and the release-day clock. The runbook says
its numbers are illustrative, not backtested: the FX conversion and the expiry day below
are assumptions to confirm against your live option chain.
"""
from datetime import date, datetime, timedelta
from math import exp, log, sqrt
from statistics import NormalDist
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

DELTA_DEFAULT = (0.60, 0.70)
DELTA_HIGH_OVX = (0.80, 0.85)
OVX_DEEPEN_ABOVE = 35.0        # strictly above: deepen to delta 0.80-0.85

# MCX Crude Oil FUTURES expiry dates, from MCX's contract launch calendar (Circular MCX/TRD/319/2025,
# 27 Jun 2025). The 100 bbl and the 10 bbl mini contracts share the same dates, so one table serves both. They are NOT a fixed day of the month: the 19th is common but
# 2026 has the 16th, 18th, 20th and 21st too (weekends and the NYMEX-linked calendar decide).
# The calendar for 2027 is not loaded: add it here when MCX publishes it.
FUTURES_EXPIRY_CALENDAR = {
    (2026, 1): date(2026, 1, 16), (2026, 2): date(2026, 2, 19), (2026, 3): date(2026, 3, 19),
    (2026, 4): date(2026, 4, 20), (2026, 5): date(2026, 5, 18), (2026, 6): date(2026, 6, 18),
    (2026, 7): date(2026, 7, 20), (2026, 8): date(2026, 8, 19), (2026, 9): date(2026, 9, 21),
    (2026, 10): date(2026, 10, 19), (2026, 11): date(2026, 11, 19), (2026, 12): date(2026, 12, 18),
}
FALLBACK_EXPIRY_DAY = 19       # a month missing from the calendar: guess the 19th (or the business day before)
OPTION_LEAD_BUSINESS_DAYS = 2  # MCX: options expire two business days before the underlying futures (its spec)
# MCX trading holidays for 2026, from MCX's Market Operations > Trading Holidays page:
# {date: (name, morning session open, evening session open)}. Morning is 9:00-17:00, evening 17:00-23:30/23:55.
# Most holidays close only the morning session and the evening (which is when the EIA report prints) trades.
# A footnote adds Muhurat trading on Sunday 8 Nov 2026 (timings to be notified) - not modelled.
# The 2027 list is not loaded: add it when MCX publishes it.
MCX_HOLIDAYS_2026 = {
    date(2026, 1, 1): ("New Year Day", True, False),
    date(2026, 1, 26): ("Republic Day", False, False),
    date(2026, 3, 3): ("Holi", False, True),
    date(2026, 3, 26): ("Shri Ram Navmi", False, True),
    date(2026, 3, 31): ("Shri Mahavir Jayanti", False, True),
    date(2026, 4, 3): ("Good Friday", False, False),
    date(2026, 4, 14): ("Dr. Baba Saheb Ambedkar Jayanti", False, True),
    date(2026, 5, 1): ("Maharashtra Day", False, True),
    date(2026, 5, 28): ("Bakri Id", False, True),
    date(2026, 6, 26): ("Moharram", False, True),
    date(2026, 9, 14): ("Ganesh Chaturthi", False, True),
    date(2026, 10, 2): ("Mahatma Gandhi Jayanti", False, False),
    date(2026, 10, 20): ("Dassera", False, True),
    date(2026, 11, 10): ("Diwali-Balipratipada", False, True),
    date(2026, 11, 24): ("Guru Nanak Jayanti", False, True),
    date(2026, 12, 25): ("Christmas", False, False),
}
# Days with BOTH sessions closed are not business days for the expiry count. A day with any session open
# counts as one (an assumption: MCX's rule for "business day" is not stated in the specifications).
HOLIDAYS = frozenset(day for day, (_, morning, evening) in MCX_HOLIDAYS_2026.items() if not morning and not evening)
STRIKE_INTERVAL = 50           # Rs per barrel between strikes (MCX option spec; the earlier pipeline also checked
                               # it against Zerodha's public instrument master: every gap was exactly 50)
ROLL_WITHIN_DAYS = 5           # runbook: current month only if MORE than 5 days remain
CONTRACT_BARRELS = {"CRUDEOIL": 100, "CRUDEOILM": 10}   # barrels per lot: the exchange's contract sizes
STOP_BRACKET_USD = (0.18, 0.25, 0.35)   # runbook stop range: min $0.18-$0.35 on the futures
FUTURES_BAND_PCT = 4.0         # MCX crude futures daily price limit; widens to 6% then 9% (MCX leaflet)
NEAR_BAND_SHARE = 0.75         # a move this close to the band is worth a warning

RELEASE_ET = (10, 30)          # EIA WPSR, 10:30 AM New York time (DST-aware)
TIME_STOP_MIN = 35             # runbook: 35 minutes post-release
HARD_EXIT_AFTER_H = 2.5        # runbook: 10:30 PM IST when the release is 8:00 PM IST
HARD_EXIT_BEFORE_CLOSE_MIN = 60   # ...which is one hour before the 23:30 close: never exit later than this
# MCX's non-agri session closes 23:30 IST while US daylight saving time is in force and 23:55 IST after it ends
# (Circular MCX/TRD/550/2026, 29 Sep 2026: 23:55 from 2 Nov 2026 to 12 Mar 2027). It follows the US clock change.
SESSION_CLOSE_US_DST = (23, 30)
SESSION_CLOSE_US_STANDARD = (23, 55)
CHOP_EXIT_MIN = 4              # options: halved from the futures runbook's 8


def target_delta(ovx):
    """(low, high, deepened): ITM delta 0.60-0.70, or 0.80-0.85 when OVX is above 35."""
    if ovx > OVX_DEEPEN_ABOVE:
        return (*DELTA_HIGH_OVX, True)
    return (*DELTA_DEFAULT, False)


def _is_business_day(day):
    return day.weekday() < 5 and day not in HOLIDAYS


def _business_days_before(day, count):
    """`day` moved back `count` business days (count 0 = the previous business day if `day` is not one)."""
    while not _is_business_day(day):
        day -= timedelta(days=1)
    for _ in range(count):
        day -= timedelta(days=1)
        while not _is_business_day(day):
            day -= timedelta(days=1)
    return day


def mcx_evening_session(day):
    """(open, reason): whether MCX's evening session (17:00 to the close) trades on `day`. The EIA report prints
    in it, so a closed evening means the signal cannot be traded that day. `reason` names the holiday or
    'weekend'; None when open. Only 2026 holidays are loaded."""
    if day.weekday() >= 5:
        return False, "weekend"
    name, _, evening_open = MCX_HOLIDAYS_2026.get(day, (None, True, True))
    return (True, None) if evening_open else (False, name)


def futures_expiry(year, month):
    """The month's futures expiry from MCX's calendar; a month not in it falls back to the 19th (or the
    previous business day), which is a guess - see `expiry_is_known`."""
    known = FUTURES_EXPIRY_CALENDAR.get((year, month))
    return known or _business_days_before(date(year, month, FALLBACK_EXPIRY_DAY), 0)


def expiry_is_known(year, month):
    """True if the month's futures expiry comes from MCX's calendar rather than the 19th fallback."""
    return (year, month) in FUTURES_EXPIRY_CALENDAR


def option_expiry(year, month):
    """The month's option expiry: OPTION_LEAD_BUSINESS_DAYS business days before the futures expiry
    (October 2026: futures Mon 19th -> options Thu 15th)."""
    return _business_days_before(futures_expiry(year, month), OPTION_LEAD_BUSINESS_DAYS)


def _expiry_on_or_after(day):
    """The first option expiry on or after `day`."""
    this_month = option_expiry(day.year, day.month)
    if day <= this_month:
        return this_month
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return option_expiry(year, month)


def pick_expiry(release_day):
    """The expiry to trade: the nearest one, unless it is `ROLL_WITHIN_DAYS` or fewer days away,
    in which case the next month (gamma-driven bid/ask blowouts near expiry)."""
    expiry = _expiry_on_or_after(release_day)
    rolled = (expiry - release_day).days <= ROLL_WITHIN_DAYS
    if rolled:
        expiry = _expiry_on_or_after(expiry + timedelta(days=1))
    return {"expiry_date": expiry.strftime("%d-%m-%Y"), "days_to_expiry": (expiry - release_day).days,
            "rolled": rolled,
            "expiry_source": "mcx_calendar" if expiry_is_known(expiry.year, expiry.month) else "assumed_19th"}


_NORMAL = NormalDist()


def black76_delta(futures, strike, iv, days, option_type):
    """Black-76 delta (r = 0) of a call (positive) or put (negative); `iv` a fraction, `days` to expiry."""
    spread = iv * sqrt(days / 365)
    d1 = (log(futures / strike) + 0.5 * spread * spread) / spread
    return _NORMAL.cdf(d1) if option_type == "CALL" else _NORMAL.cdf(d1) - 1


def strike_for_delta(futures, iv, days, delta, option_type):
    """The strike (unrounded) whose Black-76 delta magnitude is `delta` (a call: above 0.5 means below the
    futures price; a put: above 0.5 means above it)."""
    spread = iv * sqrt(days / 365)
    d1 = _NORMAL.inv_cdf(delta) if option_type == "CALL" else -_NORMAL.inv_cdf(delta)
    return futures * exp(-(d1 * spread - 0.5 * spread * spread))


def round_strike(strike):
    return int(round(strike / STRIKE_INTERVAL) * STRIKE_INTERVAL)


def strike_guidance(futures_inr, iv_pct, days, delta_range, option_type):
    """Where the target-delta strikes should sit, since no option chain is available. An ESTIMATE: Black-76
    with the CBOE OVX standing in for MCX implied volatility, the futures price approximated as WTI x USD/INR,
    rounded to the Rs 50 strike interval. Confirm every number on the live chain."""
    iv = iv_pct / 100
    guide = {"futures_level_inr": round(futures_inr), "atm_strike": round_strike(futures_inr),
             "iv_used_pct": iv_pct, "days_to_expiry": days}
    for label, delta in zip(("delta_low", "delta_high"), delta_range):
        strike = round_strike(strike_for_delta(futures_inr, iv, days, delta, option_type))
        guide[f"strike_at_{label}"] = strike
        guide[f"delta_at_{label}_strike"] = round(abs(black76_delta(futures_inr, strike, iv, days, option_type)), 2)
    return guide


def sizing(lots_by_contract, usd_inr, delta_range):
    """What the lots you trade lose in INR if the option's stop is hit, per contract and for each
    futures stop in the runbook's $0.18-$0.35 bracket, at the middle of the delta range:

        option stop (INR) = futures stop (USD) * USD/INR * delta;  loss = lots * option stop * barrels per lot

    `lots_by_contract` is {"CRUDEOIL": n, "CRUDEOILM": m} for the contracts you configured. The lot
    counts are your choice; the real stop (1.5 x 1-min ATR, or outside VWAP +/-1.5 sigma) is read off
    the chart, so the loss is shown for three of them."""
    delta = round(sum(delta_range) / 2, 3)
    return {
        "delta_used": delta,
        "contracts": {
            name: {"lots": lots, "barrels_per_lot": CONTRACT_BARRELS[name],
                   "risk_inr_by_futures_stop_usd": {
                       f"{stop:.2f}": int(round(lots * stop * usd_inr * delta * CONTRACT_BARRELS[name]))
                       for stop in STOP_BRACKET_USD}}
            for name, lots in lots_by_contract.items()
        },
    }


def band_context(move_inr, futures_inr):
    """The expected move against MCX's 4% futures price limit. `futures_inr` is the futures price in
    Rs/bbl (approximately WTI x USD/INR). The limit is measured from the previous settlement and
    the day may already have moved, so this is the move's size against a full band, not a forecast
    of what is left. A locked futures market can freeze the options too."""
    band_inr = futures_inr * FUTURES_BAND_PCT / 100
    share = abs(move_inr) / band_inr
    return {"futures_price_inr": round(futures_inr), "band_pct": FUTURES_BAND_PCT, "band_inr": round(band_inr),
            "move_pct_of_price": round(abs(move_inr) / futures_inr * 100, 2), "move_share_of_band": round(share, 2),
            "near_band": share >= NEAR_BAND_SHARE}


def session_close(day):
    """(hour, minute) IST when MCX's crude session closes on `day`: 23:30 in US daylight saving time,
    23:55 after it ends. Uses the US clock on that day, which matches the circular's 2 Nov 2026 to
    12 Mar 2027 window (the US changes on Sundays, MCX applies it from the Monday)."""
    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=NEW_YORK)
    return SESSION_CLOSE_US_DST if noon.dst() else SESSION_CLOSE_US_STANDARD


def release_schedule(release_day):
    """IST clock for the release day: the print (10:30 ET, so 20:00 IST in US summer time and 21:00 IST in
    winter), the 35-minute time stop, the session close, and the hard exit: 2.5 h after the print but never
    later than one hour before the close (22:30 in summer; 22:55 in winter, when 2.5 h would be 23:30)."""
    release = datetime(release_day.year, release_day.month, release_day.day, *RELEASE_ET, tzinfo=NEW_YORK)
    close = datetime(release_day.year, release_day.month, release_day.day, *session_close(release_day), tzinfo=IST)
    hard_exit = min(release + timedelta(hours=HARD_EXIT_AFTER_H), close - timedelta(minutes=HARD_EXIT_BEFORE_CLOSE_MIN))
    at = lambda moment: moment.astimezone(IST).strftime("%H:%M")  # noqa: E731
    return {"release_ist": at(release),
            "time_stop_ist": at(release + timedelta(minutes=TIME_STOP_MIN)),
            "hard_exit_ist": at(hard_exit),
            "session_close_ist": at(close),
            "chop_exit_min": CHOP_EXIT_MIN}
