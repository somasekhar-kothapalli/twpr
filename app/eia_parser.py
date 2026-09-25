"""Fetch the EIA Weekly Petroleum Status Report actuals and save the week's changes.

Runs Wednesday 20:00 IST and polls until the new week appears (EIA publishes at
10:30 ET). Stock *changes* are derived as this week minus last week, converted
from thousand barrels to million barrels.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
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


def main() -> int:
    """Poll the EIA API until this week's WPSR lands, then save and publish it."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR EIA WPSR parser")
    parser.add_argument("--once", action="store_true", help="single attempt, no polling")
    parser.add_argument("--week", help="expected week ending (YYYY-MM-DD); defaults to last Friday")
    args = parser.parse_args()

    try:
        api_key = os.getenv("EIA_API_KEY")
        if not api_key:
            raise RuntimeError("EIA_API_KEY not set — get a free key at eia.gov/opendata")

        expected_week = args.week or week_ending()
        existing = read_json(EIA_ACTUAL_FILE) or {}
        if existing.get("week_ending") == expected_week:
            logger.info("EIA actuals for %s already saved — nothing to do", expected_week)
            return 0

        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            report = fetch_eia_report(api_key, expected_week)
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
