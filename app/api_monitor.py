"""Capture the API (American Petroleum Institute) crude inventory change -> data/api_report.json.

One actual, no consensus exists for this report:
    api_crude_mb

The API publishes Tuesday ~16:30 ET (Wednesday ~02:00 IST). Unless --once is
given this polls until the report lands.

Crude is a dated calendar row on BOTH sites and is raced like the consensus: the first
valid value wins (the row's release date must be the one wanted). The API's Cushing,
gasoline and distillate figures are not captured: they sit behind a paywall, and the
copies on tradingeconomics lag (seen live 2026-09-30: Cushing still showed last week's
value while the other rows were new) and investing.com's events for them are dead. The
model does not use them (Cushing comes from the EIA report itself).

Run from the repo root:
    python -m app.api_monitor                      # latest due report, polling
    python -m app.api_monitor --once               # one attempt
    python -m app.api_monitor --date 22-09-2026    # a specific release (replay)
"""
import argparse
import logging
import os

from dotenv import load_dotenv

from app.utils.common import API_REPORT_FILE, ROOT, now_ist, now_utc, poll, setup_logging, write_json
from app.utils.racer import race
from app.utils.telegram import send_exception
from app.scraper.sources import SOURCE_NAMES, scraper_for, slug_for
from app.scraper.utils.calendar import current_release, row_for_release

logger = logging.getLogger("twpr.api_monitor")

SITES = ("tradingeconomics", "investing")
FIELDS = ("crude",)
RACE_TIMEOUT_S = 240
POLL_INTERVAL_S = 300
POLL_TIMEOUT_S = 4 * 60 * 60
FETCHED_AT_FORMAT = "%d-%m-%Y %H:%M"  # IST


def released_crude_row(rows, release_date, today):
    """The crude row for `release_date`, or today's current release (see
    current_release) - and it must have printed. Raises if not."""
    row = row_for_release(rows, release_date) if release_date else current_release(rows, today)
    if row is None:
        raise RuntimeError(f"no API crude row for {release_date}")
    if row["actual"] is None:
        raise RuntimeError(f"API report for {row['release_date']} not released yet")
    return row


def _job(release_date, make_scraper, today):
    def run(site, stop, ok, fail):
        slug = slug_for("api_crude", site)
        if stop.is_set():
            return
        try:
            with make_scraper(site) as scraper:
                page = scraper.fetch_page(slug)
            if page is None:
                raise RuntimeError(f"page fetch failed ({slug})")
            row = released_crude_row(page["calendar_rows"] or [], release_date, today)
        except Exception as exc:  # noqa: BLE001
            fail("crude", exc)
            return
        ok("crude", {"release_date": row["release_date"], "value": row["actual"]})
    return run


def fetch_api_report(release_date=None, sites=SITES, make_scraper=scraper_for,
                     timeout_s=RACE_TIMEOUT_S, today=None):
    """One attempt: race `sites`; return the payload (without fetched_at)."""
    # Row dates are the Tuesday in GMT/ET, so compare against today's UTC date.
    today = today or now_utc().date()
    decided = race(sites, FIELDS, _job(release_date, make_scraper, today), release_date, timeout_s,
                   what="API report value")
    site, candidate = decided["crude"]
    logger.info("crude source: %s", SOURCE_NAMES[site])
    return {"release_date": candidate["release_date"], "api_crude_mb": candidate["value"],
            "crude_source": SOURCE_NAMES[site]}


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")   # Telegram credentials for the failure alert
    parser = argparse.ArgumentParser(description="Capture the API weekly crude inventory change")
    parser.add_argument("--date", help="API release date, DD-MM-YYYY (default: the latest report due)")
    parser.add_argument("--once", action="store_true", help="a single attempt, no polling")
    parser.add_argument("--sites", nargs="+", choices=SITES, default=list(SITES))
    args = parser.parse_args(argv)
    try:
        result = poll(lambda: fetch_api_report(args.date, tuple(args.sites)),
                      args.once, POLL_INTERVAL_S, POLL_TIMEOUT_S, logger)
        payload = {**result, "fetched_at": now_ist().strftime(FETCHED_AT_FORMAT)}
        write_json(API_REPORT_FILE, payload)
        logger.info("API report %s: crude %+.3f (%s)", payload["release_date"], payload["api_crude_mb"],
                    payload["crude_source"])
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("api_monitor failed")
        send_exception("api_monitor.py", exc)
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # A losing site's daemon thread may still be inside a browser call; exit hard
    # so an abandoned Playwright thread can't hang or crash interpreter shutdown.
    os._exit(code)
