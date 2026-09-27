"""Fetch the EIA Weekly Petroleum Status Report actuals and save the week's changes.

Runs Wednesday 20:00 IST and polls until the new week appears (EIA publishes at
10:30 ET).

Source order, tried on every poll:
  1. the EIA API v2, when EIA_API_KEY is set — the publisher, so it wins
  2. tradingeconomics_scraper.fetch_eia_actuals()
  3. investing_scraper.fetch_eia_actuals()

From the API, stock *changes* are derived as this week minus last week and
converted from thousand barrels to million barrels. The scrapers publish the
changes directly, already in million barrels, but carry no refinery utilization
percentage, so that field is null when a scraper supplies the week.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from inspect import signature
from datetime import timedelta

import httpx
from dotenv import load_dotenv

from common import DATA_DIR, now_utc, read_json, setup_logging, week_ending, write_json
from petrocore_client import PetroCoreClient
from telegram_bot import send_error

load_dotenv()

logger = logging.getLogger(__name__)

EIA_ACTUAL_FILE = DATA_DIR / "eia_actual.json"

STOCKS_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
REFINERY_URL = "https://api.eia.gov/v2/petroleum/pnp/wiup/data/"

# EIA weekly series. If a live run returns no rows for one of these, the series
# id is what to check first — everything downstream depends on them.
STOCK_SERIES = {
    "crude": "WCESTUS1",  # U.S. ending stocks of crude oil, excluding SPR
    "cushing": "W_EPC0_SAX_YCUOK_MBBL",  # Cushing, OK ending stocks of crude oil
    "gasoline": "WGTSTUS1",  # U.S. ending stocks of total gasoline
    "distillate": "WDISTUS1",  # U.S. ending stocks of distillate fuel oil
}
REFINERY_SERIES = "WPULEUS3"  # Percent utilization of refinery operable capacity

# What the signal engine actually reads. refinery_util_pct is recorded only, and
# the scrapers cannot supply it, so it is deliberately not required here.
REQUIRED_FIELDS = (
    "crude_change_mb",
    "cushing_stocks_mb",
    "gasoline_change_mb",
    "distillate_change_mb",
)

POLL_INTERVAL_SECONDS = 60
POLL_TIMEOUT_SECONDS = 90 * 60
REQUEST_TIMEOUT_SECONDS = 30.0


def _fetch_series(api_key: str, url: str, series_id: str, start: str) -> list[dict]:
    """Fetch one weekly EIA series from `start`, newest first."""
    params = {
        "api_key": api_key,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": series_id,
        "start": start,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": "10",
    }
    response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    rows = response.json()["response"]["data"]
    logger.info("EIA %s: %d rows, latest period %s", series_id, len(rows),
                rows[0]["period"] if rows else "none")
    return rows


def _weekly_change_mb(rows: list[dict], series_id: str) -> tuple[str, float]:
    """Return (period, change in million barrels) from the two newest rows."""
    if len(rows) < 2:
        raise ValueError(f"{series_id}: need 2 weeks of data, got {len(rows)}")
    current, previous = rows[0], rows[1]
    if current["value"] is None or previous["value"] is None:
        raise ValueError(f"{series_id}: null value in the two newest weeks")
    # EIA reports thousand barrels; TWPR works in million barrels.
    change_mb = (float(current["value"]) - float(previous["value"])) / 1000.0
    return current["period"], change_mb


def fetch_eia_report(api_key: str, expected_week: str | None = None) -> dict | None:
    """Fetch one WPSR release. Returns None if the expected week is not published yet."""
    # Six weeks back is plenty of history to always contain two usable rows.
    start = (now_utc().date() - timedelta(weeks=6)).isoformat()

    changes: dict[str, float] = {}
    periods: set[str] = set()
    for name, series_id in STOCK_SERIES.items():
        rows = _fetch_series(api_key, STOCKS_URL, series_id, start)
        period, change_mb = _weekly_change_mb(rows, series_id)
        changes[name] = change_mb
        periods.add(period)

    if len(periods) != 1:
        logger.warning("EIA series disagree on the latest period: %s — release still landing", periods)
        return None

    period = periods.pop()
    if expected_week and period != expected_week:
        logger.info("Latest EIA period is %s, waiting for %s", period, expected_week)
        return None

    refinery_rows = _fetch_series(api_key, REFINERY_URL, REFINERY_SERIES, start)
    refinery_util = (
        float(refinery_rows[0]["value"])
        if refinery_rows and refinery_rows[0].get("value") is not None
        else None
    )

    return {
        "week_ending": period,
        "report_date": now_utc().date().isoformat(),
        "crude_change_mb": round(changes["crude"], 3),
        "cushing_stocks_mb": round(changes["cushing"], 3),
        "gasoline_change_mb": round(changes["gasoline"], 3),
        "distillate_change_mb": round(changes["distillate"], 3),
        "refinery_util_pct": refinery_util,
        "source": "eia_api",
        "released_at": now_utc().isoformat(),
    }


def _from_scraper(module_name: str, week: str) -> dict | None:
    """Try `module_name.fetch_eia_actuals(week)`. None when absent or failing."""
    try:
        module = __import__(module_name)
    except ImportError:
        logger.info("%s.py not present — skipping", module_name)
        return None

    fetcher = getattr(module, "fetch_eia_actuals", None)
    if fetcher is None:
        logger.warning("%s.py has no fetch_eia_actuals() — skipping", module_name)
        return None

    try:
        data = fetcher(week) if signature(fetcher).parameters else fetcher()
    except Exception as exc:  # noqa: BLE001 — a broken scraper must not kill the run
        logger.error("%s.fetch_eia_actuals() failed: %s", module_name, exc)
        return None

    if not data:
        return None

    missing = [f for f in REQUIRED_FIELDS if data.get(f) is None]
    if missing:
        logger.error("%s.fetch_eia_actuals() omitted %s — discarding", module_name, missing)
        return None

    return {
        "week_ending": week,
        "report_date": now_utc().date().isoformat(),
        **{f: data[f] for f in REQUIRED_FIELDS},
        "refinery_util_pct": data.get("refinery_util_pct"),
        "source": module_name.replace("_scraper", ".com"),
        "released_at": now_utc().isoformat(),
    }


def fetch_actuals(week: str, api_key: str | None) -> dict | None:
    """One attempt across every available source, in order of authority.

    The EIA API is the publisher, so it wins when a key is configured. The
    scrapers are a fallback that lets the Wednesday pipeline run without one.
    """
    if api_key:
        try:
            report = fetch_eia_report(api_key, week)
            if report is not None:
                return report
        except Exception as exc:  # noqa: BLE001 — fall through to the scrapers
            logger.error("EIA API attempt failed: %s", exc)
    else:
        logger.info("EIA_API_KEY not set — using the scrapers (refinery_util_pct will be null)")

    for module_name in ("tradingeconomics_scraper", "investing_scraper"):
        report = _from_scraper(module_name, week)
        if report is not None:
            return report

    return None


def main() -> int:
    """Poll until this week's WPSR lands, then save and publish it."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR EIA WPSR parser")
    parser.add_argument("--once", action="store_true", help="single attempt, no polling")
    parser.add_argument("--week", help="expected week ending (YYYY-MM-DD); defaults to last Friday")
    args = parser.parse_args()

    try:
        api_key = os.getenv("EIA_API_KEY")

        expected_week = args.week or week_ending()
        existing = read_json(EIA_ACTUAL_FILE) or {}
        if existing.get("week_ending") == expected_week:
            logger.info("EIA actuals for %s already saved — nothing to do", expected_week)
            return 0

        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            report = fetch_actuals(expected_week, api_key)
            if report is not None:
                break
            if args.once or time.monotonic() >= deadline:
                raise TimeoutError(f"EIA data for week ending {expected_week} never appeared")
            time.sleep(POLL_INTERVAL_SECONDS)

        write_json(EIA_ACTUAL_FILE, report)
        logger.info(
            "EIA %s: crude %+.3f mb | Cushing %+.3f mb | gasoline %+.3f mb | distillate %+.3f mb",
            report["week_ending"],
            report["crude_change_mb"],
            report["cushing_stocks_mb"],
            report["gasoline_change_mb"],
            report["distillate_change_mb"],
        )

        PetroCoreClient().post_eia_report(report)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("eia_parser.py failed")
        send_error("eia_parser.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
