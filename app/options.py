"""MCX options mechanics from docs/WPSR_MCX_OPTIONS_RUNBOOK.md. Pure: no I/O, no clock.

Strike (ITM delta), expiry gate, lot sizing and the release-day clock. The runbook says
its numbers are illustrative, not backtested: the FX conversion and the expiry day below
are assumptions to confirm against your live option chain.
"""
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

DELTA_DEFAULT = (0.60, 0.70)
DELTA_HIGH_OVX = (0.80, 0.85)
OVX_DEEPEN_ABOVE = 35.0        # strictly above: deepen to delta 0.80-0.85

FUTURES_EXPIRY_DAY = 19        # MCX crude futures expire on the 19th (README section 13)...
OPTION_LEAD_BUSINESS_DAYS = 2  # ...and the OPTIONS expire this many business days earlier. Rule inferred from
                               # one confirmed date (October 2026: options 15 Oct, futures Mon 19 Oct) plus an
                               # external review - confirm other months on your chain.
HOLIDAYS = frozenset()         # MCX trading holidays (datetime.date). Empty: only weekends are skipped, so a
                               # holiday near expiry would shift the real date by a day. Fill in from the MCX list.
ROLL_WITHIN_DAYS = 5           # runbook: current month only if MORE than 5 days remain
LOT_BARRELS = 100              # MCX crude lot
RISK_FRACTION = 0.01           # 1% of account equity per event
STOP_BRACKET_USD = (0.18, 0.25, 0.35)   # runbook stop range: min $0.18-$0.35 on the futures

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
    """The month's futures expiry: the 19th, or the previous business day if that is not one."""
    return _business_days_before(date(year, month, FUTURES_EXPIRY_DAY), 0)


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
            "rolled": rolled}


def lots_for_stop(equity_inr, stop_usd, usd_inr, delta, max_lots=None):
    """Lots so that hitting the option stop loses 1% of equity.

        option stop (INR) = futures stop (USD) * USD/INR * delta
        lots = floor(equity * 1% / (option stop * 100 bbl))
    """
    risk_per_lot = stop_usd * usd_inr * delta * LOT_BARRELS
    lots = math.floor(equity_inr * RISK_FRACTION / risk_per_lot)
    return min(lots, max_lots) if max_lots is not None else lots


def sizing(equity_inr, usd_inr, delta_range, max_lots=None):
    """Risk budget and lots for each stop in the runbook's $0.18-$0.35 bracket, at the mid delta.
    The real stop (1.5 x 1-min ATR, or outside VWAP +/-1.5 sigma) is read off the chart."""
    delta = round(sum(delta_range) / 2, 3)
    return {
        "equity_inr": equity_inr,
        "risk_inr": round(equity_inr * RISK_FRACTION),
        "delta_used": delta,
        "max_lots": max_lots,
        "lots_by_futures_stop_usd": {f"{stop:.2f}": lots_for_stop(equity_inr, stop, usd_inr, delta, max_lots)
                                     for stop in STOP_BRACKET_USD},
    }


def release_schedule(release_day):
    """IST clock for the release day: the print (10:30 ET, so 20:00 IST in US summer time and
    21:00 IST in winter), the 35-minute time stop and the hard exit 2.5 h after the print."""
    release = datetime(release_day.year, release_day.month, release_day.day, *RELEASE_ET, tzinfo=NEW_YORK)
    at = lambda moment: moment.astimezone(IST).strftime("%H:%M")  # noqa: E731
    return {"release_ist": at(release),
            "time_stop_ist": at(release + timedelta(minutes=TIME_STOP_MIN)),
            "hard_exit_ist": at(release + timedelta(hours=HARD_EXIT_AFTER_H)),
            "chop_exit_min": CHOP_EXIT_MIN}
