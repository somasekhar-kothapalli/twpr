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
import queue
import sys
import threading
import time

from app.common import DATA_DIR, now_ist, setup_logging, write_json
from app.scraper.sources import scraper_for, slug_for
from app.scraper.utils.calendar import format_date, pending_row, row_for_release

logger = logging.getLogger("twpr.consensus_fetcher")

CONSENSUS_FILE = DATA_DIR / "consensus.json"
SITES = ("tradingeconomics", "investing")
SOURCE_NAMES = {"tradingeconomics": "tradingeconomics", "investing": "investing.com"}
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


def decide(candidates, release_date=None):
    """First valid candidate per indicator, all for one release date.

    `candidates` = {name: [(site, candidate), ...]} in arrival order. The date is
    anchored by `release_date` if given, else by the crude winner; until it is
    known, gasoline/distillate stay undecided. Returns {name: (site, candidate)}.
    """
    anchor = format_date(release_date) if release_date else None
    decided = {}
    for name in INDICATORS:
        if anchor is None and name != "crude":
            break
        for site, cand in candidates.get(name, []):
            if anchor is None or cand["release_date"] == anchor:
                decided[name] = (site, cand)
                anchor = anchor or cand["release_date"]
                break
    return decided


def _worker(site, release_date, stop, make_scraper, out):
    """Walk the indicators on one site, reporting each to `out` as it lands."""
    try:
        gap = getattr(make_scraper(site), "session_gap_s", 0)
        for i, name in enumerate(INDICATORS):
            if i and stop.wait(gap):  # paced sessions; wakes early if the race ended
                return
            try:
                out.put((site, name, fetch_candidate(site, name, release_date, stop, make_scraper), None))
            except Exception as exc:  # noqa: BLE001 - one indicator failing must not sink the site
                out.put((site, name, None, exc))
    finally:
        out.put((site, None, None, None))  # site finished


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
    out = queue.Queue()
    stop = threading.Event()
    for site in sites:
        threading.Thread(target=_worker, args=(site, release_date, stop, make_scraper, out),
                         daemon=True, name=site).start()

    candidates = {name: [] for name in INDICATORS}
    errors = {name: {} for name in INDICATORS}
    running = len(sites)
    deadline = time.monotonic() + timeout_s
    while running:
        try:
            site, name, cand, exc = out.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if name is None:
            running -= 1
            continue
        if exc is not None:
            errors[name][site] = exc
            logger.warning("%s / %s failed: %s", site, name, exc)
            continue
        candidates[name].append((site, cand))
        decided = decide(candidates, release_date)
        if len(decided) == len(INDICATORS):
            stop.set()
            result = _payload(decided)
            logger.info("sources: %s", {n: result[f"{n}_source"] for n in INDICATORS})
            return result

    stop.set()
    decided = decide(candidates, release_date)
    problems = []
    for name in INDICATORS:
        if name in decided:
            continue
        why = "; ".join(f"{s}: {e}" for s, e in errors[name].items())
        if candidates[name] and not why:
            why = "values found but for a different release date than the crude winner"
        problems.append(f"{name} ({why or f'no answer within {timeout_s}s'})")
    raise RuntimeError("no valid consensus for " + ", ".join(problems))


def main(argv=None):
    setup_logging()
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
    except Exception:  # noqa: BLE001 - top-level guard
        logger.exception("consensus_fetcher failed")
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # The losing site's daemon thread may still be inside a browser call; exit
    # hard so an abandoned Playwright thread can't hang or crash interpreter shutdown.
    os._exit(code)
