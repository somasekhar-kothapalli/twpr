"""Capture the API (American Petroleum Institute) private inventory report -> data/api_report.json.

Four actuals, no consensus exists for this report:
    api_crude_mb, api_cushing_mb, api_gasoline_mb, api_distillate_mb

The API publishes Tuesday ~16:30 ET (Wednesday ~02:00 IST). Unless --once is
given this polls until the report lands.

Sources:
- crude is a dated calendar row on BOTH sites and is raced like the consensus:
  the first valid value wins (the row's release date must be the one wanted).
- cushing / gasoline / distillate exist only on tradingeconomics, as an UNDATED
  2-decimal "Related" snapshot (investing.com's events for them are dead). Reading
  it blind could silently return last week's numbers, so it is taken from the same
  page load as the dated crude row and used only if its crude value matches that
  row's actual. Those three are therefore 2-decimal; crude keeps 3.

Run from the repo root:
    python -m app.api_monitor                      # latest due report, polling
    python -m app.api_monitor --once               # one attempt
    python -m app.api_monitor --date 22-09-2026    # a specific release (replay)
"""
import argparse
import json
import logging
import os

from dotenv import load_dotenv

from app.utils.common import API_REPORT_FILE, ROOT, now_ist, now_utc, parse_release_date, poll, setup_logging, write_json
from app.utils.racer import race
from app.utils.telegram import send_exception
from app.scraper.sites.tradingeconomics import parse_related_table
from app.scraper.sources import SOURCE_NAMES, scraper_for, slug_for
from app.scraper.utils.calendar import current_release, row_for_release

logger = logging.getLogger("twpr.api_monitor")

SITES = ("tradingeconomics", "investing")
FIELDS = ("crude", "cushing", "gasoline", "distillate")  # crude first: it anchors the release date
LEGS = FIELDS[1:]
SUPPLIES = {"tradingeconomics": FIELDS, "investing": ("crude",)}  # fields each site can deliver
RELATED_NAMES = {
    "crude": "API Crude Oil Stock Change",
    "cushing": "API Cushing Number",
    "gasoline": "API Gasoline Stocks",
    "distillate": "API Distillate Stocks",
}
ROUNDING_TOLERANCE = 0.0051  # the Related snapshot is rounded to 2 decimals
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


def _is_previous_week(release_date, last_release_date):
    days = (parse_release_date(release_date) - parse_release_date(last_release_date)).days
    return 4 <= days <= 10


def legs_from_snapshot(related, crude_row, last_report=None):
    """{leg: value or Exception} from TE's undated Related snapshot, trusted only
    when its crude value matches the dated crude row (i.e. it is the same release).

    TE updates the four rows at different moments (seen live 2026-09-30: crude, gasoline and
    distillate were new, Cushing still last week's +2.08), so crude matching proves nothing
    about the other rows. When `last_report` is the previous week's file, a fresh leg's
    "previous" must equal that report's value for the leg; a leg whose "previous" differs is
    still showing last week's number and is rejected (the poll retries)."""
    crude = (related.get(RELATED_NAMES["crude"]) or {}).get("actual_mb")
    if crude is None:
        error = ValueError("Related snapshot has no crude value to verify against")
    elif abs(crude - crude_row["actual"]) > ROUNDING_TOLERANCE:
        error = ValueError(
            f"Related snapshot is a different release (crude {crude} vs dated {crude_row['actual']})")
    else:
        error = None
    check_previous = bool(last_report and last_report.get("release_date")
                          and _is_previous_week(crude_row["release_date"], last_report["release_date"]))
    legs = {}
    for leg in LEGS:
        row = related.get(RELATED_NAMES[leg]) or {}
        value, previous, last = row.get("actual_mb"), row.get("previous_mb"), (last_report or {}).get(f"api_{leg}_mb")
        if error:
            legs[leg] = error
        elif value is None:
            legs[leg] = ValueError(f"{leg} missing from Related snapshot")
        elif check_previous and last is not None and previous is not None and abs(previous - last) > ROUNDING_TOLERANCE:
            legs[leg] = ValueError(f"{leg} still shows last week's value (its previous {previous} is not the last "
                                   f"report's {last}): TE has not updated it yet")
        else:
            legs[leg] = value
    return legs


def _job(release_date, make_scraper, today, last_report=None):
    def run(site, stop, ok, fail):
        slug = slug_for("api_crude", site)
        if stop.is_set():
            return
        try:
            with make_scraper(site) as scraper:
                page, soup = scraper.fetch_with_soup(slug)
            if page is None:
                raise RuntimeError(f"page fetch failed ({slug})")
            row = released_crude_row(page["calendar_rows"] or [], release_date, today)
        except Exception as exc:  # noqa: BLE001
            for field in SUPPLIES[site]:
                fail(field, exc)
            return

        ok("crude", {"release_date": row["release_date"], "value": row["actual"]})
        if "cushing" in SUPPLIES[site]:
            for leg, value in legs_from_snapshot(parse_related_table(soup), row, last_report).items():
                if isinstance(value, Exception):
                    fail(leg, value)
                else:
                    ok(leg, {"release_date": row["release_date"], "value": value})
    return run


def fetch_api_report(release_date=None, sites=SITES, make_scraper=scraper_for,
                     timeout_s=RACE_TIMEOUT_S, today=None, last_report=None):
    """One attempt: race `sites`; return the payload (without fetched_at). `last_report` is
    the previous week's api_report.json (a dict), used to catch legs TE has not updated."""
    # Row dates are the Tuesday in GMT/ET, so compare against today's UTC date.
    today = today or now_utc().date()
    decided = race(sites, FIELDS, _job(release_date, make_scraper, today, last_report), release_date, timeout_s,
                   what="API report value")
    result = {"release_date": decided["crude"][1]["release_date"]}
    for field in FIELDS:
        result[f"api_{field}_mb"] = decided[field][1]["value"]
    for field in FIELDS:
        result[f"{field}_source"] = SOURCE_NAMES[decided[field][0]]
    logger.info("sources: %s", {f: result[f"{f}_source"] for f in FIELDS})
    return result


def _read_last_report():
    """The existing api_report.json (last week's, until this run overwrites it), or None."""
    try:
        return json.loads(API_REPORT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")   # Telegram credentials for the failure alert
    parser = argparse.ArgumentParser(description="Capture the API weekly inventory report")
    parser.add_argument("--date", help="API release date, DD-MM-YYYY (default: the latest report due)")
    parser.add_argument("--once", action="store_true", help="a single attempt, no polling")
    parser.add_argument("--sites", nargs="+", choices=SITES, default=list(SITES))
    args = parser.parse_args(argv)
    try:
        last_report = _read_last_report()
        result = poll(lambda: fetch_api_report(args.date, tuple(args.sites), last_report=last_report),
                      args.once, POLL_INTERVAL_S, POLL_TIMEOUT_S, logger)
        payload = {**result, "fetched_at": now_ist().strftime(FETCHED_AT_FORMAT)}
        write_json(API_REPORT_FILE, payload)
        logger.info(
            "API report %s: crude %+.3f | cushing %+.2f | gasoline %+.2f | distillate %+.2f",
            payload["release_date"], payload["api_crude_mb"], payload["api_cushing_mb"],
            payload["api_gasoline_mb"], payload["api_distillate_mb"],
        )
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
