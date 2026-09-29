"""Capture the EIA Weekly Petroleum Status Report actuals -> data/eia_actuals.json.

Released Wednesday 10:30 ET (20:00 IST). Unless --once is given this polls until
the report lands. Five outputs: crude / Cushing / gasoline / distillate stock
CHANGES in million barrels (negative = draw), plus the refinery utilisation change.

Both sites are scraped at the same time and the WHOLE report comes from one of
them: the first site to return a complete, consistent report wins (`source`,
`won_race`). A site missing any of the four changes, or with them on different
release dates, is not a candidate. Nothing is written unless all four are valid.

There is no refinery utilisation LEVEL: it is a percentage (e.g. 93.5) that neither
site carries (tradingeconomics has only refinery crude runs in barrels;
investing.com's event 1961 is the week-over-week CHANGE in utilisation, which must
not be passed off as a level). The change is written as refinery_util_change_pct -
best effort: only investing.com has it, so it is an optional field of the race
(investing fetches it first and the race waits `REFINERY_GRACE_S` for it once the
report is decided; null if it didn't arrive in time). Nothing downstream reads it.

Run from the repo root:
    python -m app.eia_actuals                      # today's report, polling
    python -m app.eia_actuals --once               # one attempt
    python -m app.eia_actuals --date 23-09-2026 --once   # replay a release
"""
import argparse
import logging
import os
import sys

from app.utils.common import DATA_DIR, now_ist, now_utc, poll, setup_logging, write_json
from app.utils.racer import race
from app.scraper.sources import SOURCE_NAMES, scraper_for, slug_for
from app.scraper.utils.calendar import current_release, row_for_release

logger = logging.getLogger("twpr.eia_actuals")

EIA_ACTUALS_FILE = DATA_DIR / "eia_actuals.json"
SITES = ("tradingeconomics", "investing")
LEGS = ("crude", "cushing", "gasoline", "distillate")  # crude first: it anchors the release date
RACE_TIMEOUT_S = 420  # investing.com needs one paced browser session per leg
REFINERY_GRACE_S = 30  # extra wait for the optional refinery change once the report is decided
POLL_INTERVAL_S = 60
POLL_TIMEOUT_S = 90 * 60
TIME_FORMAT = "%d-%m-%Y %H:%M"  # IST


def _leg_row(site, leg, release_date, today, stop, make_scraper):
    """The released row for one leg: today's current release for crude (which sets
    the date), that same date for the others. Raises if absent or not printed."""
    if stop.is_set():
        raise InterruptedError("race over")
    slug = slug_for(f"eia_{leg}", site)
    if slug is None:
        raise LookupError(f"{site} has no slug for eia_{leg}")
    with make_scraper(site) as scraper:
        page = scraper.fetch_page(slug)
    if page is None:
        raise RuntimeError(f"{leg}: page fetch failed ({slug})")
    rows = page["calendar_rows"] or []
    row = row_for_release(rows, release_date) if release_date else current_release(rows, today)
    if row is None:
        raise RuntimeError(f"{leg}: no row for release {release_date}")
    if row["actual"] is None:
        raise RuntimeError(f"{leg}: EIA report for {row['release_date']} not released yet")
    return row


def fetch_candidate(site, release_date, today, stop, make_scraper):
    """One site's complete report {release_date, <leg>_change_mb x4}, or raise."""
    gap = getattr(make_scraper(site), "session_gap_s", 0)
    result = {}
    for i, leg in enumerate(LEGS):
        if i and stop.wait(gap):  # paced sessions; wakes early if the race ended
            raise InterruptedError("race over")
        row = _leg_row(site, leg, release_date, today, stop, make_scraper)
        release_date = release_date or row["release_date"]  # crude anchors the rest
        result.setdefault("release_date", row["release_date"])
        result[f"{leg}_change_mb"] = row["actual"]
    return result


def refinery_candidates(site, stop, make_scraper):
    """One candidate per printed row of the refinery utilisation-change page. The
    release date isn't known yet (the report race decides it), so offer them all
    and let decide() pick the row matching the winner."""
    slug = slug_for("eia_refinery", site)
    if stop.is_set():
        raise InterruptedError("race over")
    with make_scraper(site) as scraper:
        page = scraper.fetch_page(slug)
    if page is None:
        raise RuntimeError(f"refinery: page fetch failed ({slug})")
    return [{"release_date": r["release_date"], "value": r["actual"]}
            for r in page["calendar_rows"] or [] if r["actual"] is not None]


def fetch_eia_actuals(release_date=None, sites=SITES, make_scraper=scraper_for,
                     timeout_s=RACE_TIMEOUT_S, today=None, grace_s=REFINERY_GRACE_S):
    """One attempt: race `sites` for the whole report; return the payload (no timestamps)."""
    # Row dates are GMT/ET, so compare against today's UTC date.
    today = today or now_utc().date()

    def job(site, stop, ok, fail):
        gap = getattr(make_scraper(site), "session_gap_s", 0)
        if slug_for("eia_refinery", site):  # first, so it lands before a fast site wins
            try:
                for cand in refinery_candidates(site, stop, make_scraper):
                    ok("refinery", cand)
            except Exception as exc:  # noqa: BLE001 - optional; must never sink the report
                fail("refinery", exc)
            if stop.wait(gap):
                return
        try:
            ok("report", fetch_candidate(site, release_date, today, stop, make_scraper))
        except Exception as exc:  # noqa: BLE001 - reported to the race, never lost
            fail("report", exc)

    decided = race(sites, ("report", "refinery"), job, release_date, timeout_s, what="EIA actuals",
                   optional=("refinery",), grace_s=grace_s)
    site, candidate = decided["report"]
    refinery = decided["refinery"][1]["value"] if "refinery" in decided else None
    logger.info("%s returned the complete report first; refinery change: %s", site, refinery)
    return {
        **candidate,
        "refinery_util_change_pct": refinery,
        "source": SOURCE_NAMES[site],
        "won_race": len(sites) > 1,  # False when nobody else was racing (e.g. --sites one)
    }


def main(argv=None):
    setup_logging()
    parser = argparse.ArgumentParser(description="Capture the EIA weekly petroleum report actuals")
    parser.add_argument("--date", help="EIA release date, DD-MM-YYYY (default: today's report)")
    parser.add_argument("--once", action="store_true", help="a single attempt, no polling")
    parser.add_argument("--sites", nargs="+", choices=SITES, default=list(SITES))
    args = parser.parse_args(argv)
    try:
        result = poll(lambda: fetch_eia_actuals(args.date, tuple(args.sites)),
                      args.once, POLL_INTERVAL_S, POLL_TIMEOUT_S, logger)
        released_at = now_ist().strftime(TIME_FORMAT)  # when the numbers were first seen
        payload = {
            "release_date": result["release_date"],
            "released_at": released_at,
            "crude_change_mb": result["crude_change_mb"],
            "cushing_change_mb": result["cushing_change_mb"],
            "gasoline_change_mb": result["gasoline_change_mb"],
            "distillate_change_mb": result["distillate_change_mb"],
            "refinery_util_change_pct": result["refinery_util_change_pct"],
            "source": result["source"],
            "won_race": result["won_race"],
            "fetched_at": now_ist().strftime(TIME_FORMAT),
        }
        write_json(EIA_ACTUALS_FILE, payload)
        logger.info(
            "EIA %s (%s): crude %+.3f | cushing %+.3f | gasoline %+.3f | distillate %+.3f",
            payload["release_date"], payload["source"], payload["crude_change_mb"],
            payload["cushing_change_mb"], payload["gasoline_change_mb"], payload["distillate_change_mb"],
        )
        return 0
    except Exception:  # noqa: BLE001 - top-level guard
        logger.exception("eia_actuals failed")
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # A losing site's daemon thread may still be inside a browser call; exit hard
    # so an abandoned Playwright thread can't hang or crash interpreter shutdown.
    os._exit(code)
