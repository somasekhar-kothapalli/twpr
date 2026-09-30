"""MCX options mechanics from docs/WPSR_MCX_OPTIONS_RUNBOOK.md. Pure: no I/O, no clock.

Strike (ITM delta), expiry gate, the loss per lot and the release-day clock. The runbook says
its numbers are illustrative, not backtested: the FX conversion and the expiry day below
are assumptions to confirm against your live option chain.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

DELTA_DEFAULT = (0.60, 0.70)
DELTA_HIGH_OVX = (0.80, 0.85)
OVX_DEEPEN_ABOVE = 35.0        # strictly above: deepen to delta 0.80-0.85

# MCX Crude Oil (100 bbl) FUTURES expiry dates, from MCX's contract launch calendar
# (Circular MCX/TRD/319/2025, 27 Jun 2025). They are NOT a fixed day of the month: the 19th is common but
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
HOLIDAYS = frozenset()         # MCX trading holidays (datetime.date). Empty: only weekends are skipped, so a
                               # holiday near expiry would shift the real date by a day. Fill in from the MCX list.
ROLL_WITHIN_DAYS = 5           # runbook: current month only if MORE than 5 days remain
CONTRACT_BARRELS = {"CRUDEOIL": 100, "CRUDEOILM": 10}   # barrels per lot: the exchange's contract sizes
                               # (CRUDEOILM is the mini contract; confirm it is what you actually trade)
STOP_BRACKET_USD = (0.18, 0.25, 0.35)   # runbook stop range: min $0.18-$0.35 on the futures
FUTURES_BAND_PCT = 4.0         # MCX crude futures daily price limit; widens to 6% then 9% (MCX leaflet)
NEAR_BAND_SHARE = 0.75         # a move this close to the band is worth a warning

RELEASE_ET = (10, 30)          # EIA WPSR, 10:30 AM New York time (DST-aware)
TIME_STOP_MIN = 35             # runbook: 35 minutes post-release
HARD_EXIT_AFTER_H = 2.5        # runbook: 10:30 PM IST when the release is 8:00 PM IST
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


def release_schedule(release_day):
    """IST clock for the release day: the print (10:30 ET, so 20:00 IST in US summer time and
    21:00 IST in winter), the 35-minute time stop and the hard exit 2.5 h after the print."""
    release = datetime(release_day.year, release_day.month, release_day.day, *RELEASE_ET, tzinfo=NEW_YORK)
    at = lambda moment: moment.astimezone(IST).strftime("%H:%M")  # noqa: E731
    return {"release_ist": at(release),
            "time_stop_ist": at(release + timedelta(minutes=TIME_STOP_MIN)),
            "hard_exit_ist": at(release + timedelta(hours=HARD_EXIT_AFTER_H)),
            "chop_exit_min": CHOP_EXIT_MIN}
