"""Fetch the analyst consensus for the upcoming EIA report -> data/consensus.json.

Both sites are scraped at the same time, one thread and browser each, and every
indicator (crude, gasoline, distillate) is won independently: the first site to
return a valid value for it wins, and the output records which site that was.
A site that can't deliver an indicator (blocked, page changed, consensus not
posted yet) simply loses that race; the other site still gets its turn. All
winners must be for the same release date. If any indicator ends with no valid
value this fails loudly - the consensus is the baseline of every deviation, so
a wrong or partial number is worse than none.

Run from the repo root:
    python -m app.consensus_fetcher                      # next unreleased EIA report
    python -m app.consensus_fetcher --date 23-09-2026    # a specific release date
    python -m app.consensus_fetcher --sites tradingeconomics
"""
import argparse
import logging
import os

from dotenv import load_dotenv

from app.utils.common import CONSENSUS_FILE, ROOT, now_ist, setup_logging, write_json
from app.utils.racer import race
from app.utils.telegram import send_exception
from app.scraper.sources import SOURCE_NAMES, scraper_for, slug_for
from app.scraper.utils.calendar import pending_row, row_for_release

logger = logging.getLogger("twpr.consensus_fetcher")

SITES = ("tradingeconomics", "investing")
INDICATORS = ("crude", "gasoline", "distillate")  # crude first: it carries crude_previous_mb
RACE_TIMEOUT_S = 480  # investing.com needs one paced browser session per indicator
FETCHED_AT_FORMAT = "%d-%m-%Y %H:%M"  # IST


def fetch_candidate(site, name, release_date, stop, make_scraper):
    """One site's answer for one indicator: {release_date, consensus, previous}, or raise."""
    slug = slug_for(f"eia_{name}", site)
    if slug is None:
        raise LookupError(f"{site} has no slug for eia_{name}")
    if stop.is_set():
        raise InterruptedError("race over")
    with make_scraper(site) as scraper:
        page = scraper.fetch_page(slug)
    if page is None:
        raise RuntimeError(f"page fetch failed ({slug})")

    rows = page["calendar_rows"] or []
    row = row_for_release(rows, release_date) if release_date else pending_row(rows)
    if row is None:
        raise RuntimeError(f"no row for release {release_date or '(next unreleased)'}")
    if row["consensus"] is None:
        raise ValueError(f"consensus for {row['release_date']} not posted yet")
    if name == "crude" and row["previous"] is None:
        raise ValueError(f"crude previous missing for {row['release_date']}")
    return {"release_date": row["release_date"], "consensus": row["consensus"], "previous": row["previous"]}


def _job(release_date, make_scraper):
    """A site's job: walk the indicators, reporting each as it lands."""
    def run(site, stop, ok, fail):
        gap = getattr(make_scraper(site), "session_gap_s", 0)
        for i, name in enumerate(INDICATORS):
            if i and stop.wait(gap):  # paced sessions; wakes early if the race ended
                return
            try:
                ok(name, fetch_candidate(site, name, release_date, stop, make_scraper))
            except Exception as exc:  # noqa: BLE001 - one indicator failing must not sink the site
                fail(name, exc)
    return run


def _payload(decided):
    crude = decided["crude"][1]
    out = {
        "release_date": crude["release_date"],
        "crude_consensus_mb": crude["consensus"],
        "gasoline_consensus_mb": decided["gasoline"][1]["consensus"],
        "distillate_consensus_mb": decided["distillate"][1]["consensus"],
        "crude_previous_mb": crude["previous"],
    }
    for name in INDICATORS:
        out[f"{name}_source"] = SOURCE_NAMES[decided[name][0]]
    return out


def fetch_consensus(release_date=None, sites=SITES, make_scraper=scraper_for, timeout_s=RACE_TIMEOUT_S):
    """Race `sites` per indicator; return the payload (without fetched_at)."""
    decided = race(sites, INDICATORS, _job(release_date, make_scraper), release_date, timeout_s, what="consensus")
    result = _payload(decided)
    logger.info("sources: %s", {n: result[f"{n}_source"] for n in INDICATORS})
    return result


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")   # Telegram credentials for the failure alert
    parser = argparse.ArgumentParser(description="Fetch the EIA consensus (first valid source per indicator wins)")
    parser.add_argument("--date", help="EIA release date, DD-MM-YYYY (default: the next unreleased report)")
    parser.add_argument("--sites", nargs="+", choices=SITES, default=list(SITES))
    args = parser.parse_args(argv)
    try:
        payload = {**fetch_consensus(args.date, tuple(args.sites)),
                   "fetched_at": now_ist().strftime(FETCHED_AT_FORMAT)}
        write_json(CONSENSUS_FILE, payload)
        logger.info(
            "consensus for %s: crude %+.3f | gasoline %+.3f | distillate %+.3f | crude previous %+.3f",
            payload["release_date"], payload["crude_consensus_mb"], payload["gasoline_consensus_mb"],
            payload["distillate_consensus_mb"], payload["crude_previous_mb"],
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("consensus_fetcher failed")
        send_exception("consensus_fetcher.py", exc)
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # The losing site's daemon thread may still be inside a browser call; exit
    # hard so an abandoned Playwright thread can't hang or crash interpreter shutdown.
    os._exit(code)
