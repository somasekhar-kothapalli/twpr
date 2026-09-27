"""MCX CrudeOil option expiry calendar and the days-to-expiry gate.

TWPR buys the near-month option. MCX CRUDEOIL options expire **two to four days
before** the futures they settle into (observed 2026-09-27: options 15 Oct vs
futures 19 Oct, 17 Nov vs 19 Nov, 16 Dec vs 18 Dec), so a Wednesday can land one
or two days from expiry while the futures contract still looks like near month.

That matters because the setup is an options **buyer** with fixed percentage
exits over a 2.5-hour hold. A near-dead option has a tiny premium and enormous
gamma, so a -40% stop fires on a trivial MCX move, and the bid-ask on it can take
more than the stop would. It is not the same instrument the exits were calibrated
for. Below `MIN_DAYS_TO_EXPIRY` the week is skipped.

Expiries come from Zerodha's public instrument master, cached in
`data/expiries.json` so the Wednesday signal path reads a file rather than the
network. `market_data.py` refreshes it on its daily run; a signal-time miss falls
back to fetching once.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime

import httpx

from common import DATA_DIR, now_utc, read_json, write_json

logger = logging.getLogger(__name__)

EXPIRIES_FILE = DATA_DIR / "expiries.json"

# Zerodha's instrument master: public, no auth, every live contract. MCX's own
# site answers 403 to plain HTTP and to a headless browser alike.
INSTRUMENTS_URL = "https://api.kite.trade/instruments"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT_SECONDS = 60.0

EXCHANGE = "MCX"
UNDERLYING = "CRUDEOIL"

# Skip the week when the near-month option has fewer than this many calendar days
# left. Three keeps the option behaving like an option through the hold and into
# the next session, and costs roughly 7 of 52 Wednesdays.
MIN_DAYS_TO_EXPIRY = 3


def fetch_expiries() -> list[str]:
    """Live MCX CrudeOil option expiry dates, soonest first."""
    response = httpx.get(
        INSTRUMENTS_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
    )
    response.raise_for_status()

    expiries = {
        row["expiry"]
        for row in csv.DictReader(io.StringIO(response.text))
        if row["exchange"] == EXCHANGE
        and row["name"] == UNDERLYING
        and row["instrument_type"] in ("CE", "PE")
        and row["expiry"]
    }
    if not expiries:
        raise ValueError(f"no {UNDERLYING} option expiries listed on {EXCHANGE}")

    return sorted(expiries)


def refresh_cache() -> list[str] | None:
    """Fetch and store the expiry calendar. Returns None on failure, never raises."""
    try:
        expiries = fetch_expiries()
    except Exception as exc:  # noqa: BLE001 — a stale calendar beats a failed run
        logger.error("Could not refresh the option expiry calendar: %s", exc)
        return None

    write_json(
        EXPIRIES_FILE,
        {"expiries": expiries, "source": "kite_instruments", "fetched_at": now_utc().isoformat()},
    )
    logger.info("Option expiry calendar refreshed: %s", expiries[:4])
    return expiries


def _cached_expiries(on: date) -> list[str] | None:
    """Cached expiries, or None when absent or too old to cover `on`."""
    cached = read_json(EXPIRIES_FILE) or {}
    expiries = cached.get("expiries") or []
    if not expiries:
        return None

    # Every listed expiry already gone means the calendar predates this month.
    if max(expiries) < on.isoformat():
        logger.warning("Expiry calendar is stale (latest %s, need %s)", max(expiries), on)
        return None
    return expiries


def next_expiry(on: date | None = None, allow_fetch: bool = True) -> date | None:
    """The first option expiry on or after `on`. None when it cannot be determined."""
    on = on or now_utc().date()

    expiries = _cached_expiries(on)
    if expiries is None and allow_fetch:
        logger.info("Expiry calendar missing or stale — fetching")
        refreshed = refresh_cache()
        expiries = refreshed if refreshed else None
    if not expiries:
        return None

    upcoming = [e for e in expiries if e >= on.isoformat()]
    if not upcoming:
        logger.warning("No option expiry on or after %s in the calendar", on)
        return None
    return date.fromisoformat(upcoming[0])


def days_to_expiry(on: date | None = None, allow_fetch: bool = True) -> tuple[date | None, int | None]:
    """(near-month option expiry, calendar days until it) as of `on`."""
    on = on or now_utc().date()
    expiry = next_expiry(on, allow_fetch=allow_fetch)
    if expiry is None:
        return None, None
    return expiry, (expiry - on).days


def is_tradeable(days: int | None) -> bool:
    """Whether the near-month option has enough life left to trade.

    An unknown number does **not** block the trade: a missing calendar is an
    infrastructure problem, and failing closed would silently cancel a week over
    a cache miss. The caller warns loudly instead and the number reaches the alert.
    """
    if days is None:
        return True
    return days >= MIN_DAYS_TO_EXPIRY


def describe(expiry: date | None, days: int | None) -> str:
    """One line about the expiry position, for a log or a risk note."""
    if expiry is None or days is None:
        return (
            "Option expiry unknown — the days-to-expiry guard could not run, so "
            "check the option chain before entering"
        )
    if days < MIN_DAYS_TO_EXPIRY:
        return f"Near-month option expires {expiry} — {days} day(s) left, under the {MIN_DAYS_TO_EXPIRY}-day floor"
    return f"Near-month option expires {expiry} — {days} days left"


def parse_date(value: str | date | None) -> date | None:
    """Coerce an ISO string or datetime to a date. None when unusable."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
