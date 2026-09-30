"""Weekly consensus-surprise history -> data/surprise_history.json (input to sigma_forecast).

One row per EIA release: crude / gasoline / distillate surprise = actual - consensus,
million barrels. app.model.sigma_forecast takes the std dev of the last 12 weekly TLS.

    python -m app.surprise_history --backfill   # one-off: past weeks from investing.com
    python -m app.surprise_history              # show the file and the current sigma

--backfill reads investing.com (its calendar keeps ~10 past releases with actual AND
consensus; tradingeconomics keeps ~3), one paced browser session per indicator. Its
consensus panel differs a little from the one tradingeconomics supplies live, which
only affects the size of the past surprises. Rows already in the file are never
overwritten. The engine appends each new week itself (record_week).
"""
import argparse
import json
import logging
import os
import time
from datetime import datetime

from app.model import history_tls, sigma_forecast
from app.scraper.sources import scraper_for, slug_for
from dotenv import load_dotenv

from app.utils.common import DATE_FORMAT, ROOT, SURPRISE_HISTORY_FILE, setup_logging, write_json
from app.utils.telegram import send_exception

logger = logging.getLogger("twpr.surprise_history")

SITE = "investing"
LEGS = (("crude", "eia_crude"), ("gasoline", "eia_gasoline"), ("distillate", "eia_distillate"))


def load_history(path=None):
    path = path or SURPRISE_HISTORY_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def merge(existing, new):
    """`existing` plus any `new` rows for release dates it lacks, oldest first."""
    have = {r["release_date"] for r in existing}
    rows = existing + [r for r in new if r["release_date"] not in have]
    return sorted(rows, key=lambda r: datetime.strptime(r["release_date"], DATE_FORMAT))


def rows_from_pages(rows_by_leg):
    """History rows for every release date where all three legs have an actual and a consensus.
    `rows_by_leg` maps 'crude'/'gasoline'/'distillate' to scraper calendar rows."""
    by_date = {}
    for leg, rows in rows_by_leg.items():
        for row in rows or []:
            if row["actual"] is not None and row["consensus"] is not None:
                by_date.setdefault(row["release_date"], {})[leg] = round(row["actual"] - row["consensus"], 3)
    return [{"release_date": date, **{f"{leg}_surprise_mb": legs[leg] for leg, _ in LEGS}}
            for date, legs in by_date.items() if len(legs) == len(LEGS)]


def record_week(consensus, actuals, path=None):
    """Append this week's surprises (consensus.json + eia_actuals.json dicts) unless already there.
    Returns the full history."""
    row = {"release_date": actuals["release_date"],
           "crude_surprise_mb": round(actuals["crude_change_mb"] - consensus["crude_consensus_mb"], 3),
           "gasoline_surprise_mb": round(actuals["gasoline_change_mb"] - consensus["gasoline_consensus_mb"], 3),
           "distillate_surprise_mb": round(actuals["distillate_change_mb"] - consensus["distillate_consensus_mb"], 3)}
    existing = load_history(path)
    merged = merge(existing, [row])
    if len(merged) != len(existing):
        write_json(path or SURPRISE_HISTORY_FILE, merged)
    return merged


def backfill(make_scraper=scraper_for, sleep=time.sleep):
    """Fetch the three investing.com calendars and return their combined surprise rows."""
    pages = {}
    for i, (leg, indicator) in enumerate(LEGS):
        with make_scraper(SITE) as scraper:
            if i:
                sleep(getattr(scraper, "session_gap_s", 0))   # it 403s back-to-back sessions
            page = scraper.fetch_page(slug_for(indicator, SITE))
        if page is None or not page["calendar_rows"]:
            raise RuntimeError(f"{SITE} returned no calendar for {indicator}")
        pages[leg] = page["calendar_rows"]
    rows = rows_from_pages(pages)
    if not rows:
        raise RuntimeError("no release had actual and consensus on all three indicators")
    return rows


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")   # Telegram credentials for the failure alert
    parser = argparse.ArgumentParser(description="Weekly consensus-surprise history")
    parser.add_argument("--backfill", action="store_true", help="add past weeks from investing.com")
    args = parser.parse_args(argv)
    try:
        history = load_history()
        if args.backfill:
            history = merge(history, backfill())
            write_json(SURPRISE_HISTORY_FILE, history)
        for row in history:
            logger.info("%s crude %+.3f | gasoline %+.3f | distillate %+.3f | TLS %+.3f", row["release_date"],
                        row["crude_surprise_mb"], row["gasoline_surprise_mb"], row["distillate_surprise_mb"],
                        history_tls(row))
        try:
            logger.info("%d weeks | sigma_forecast %.3f mb", len(history), sigma_forecast(history))
        except ValueError as exc:
            logger.warning("%d weeks | %s", len(history), exc)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level guard
        logger.exception("surprise_history failed")
        send_exception("surprise_history.py", exc)
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    os._exit(code)   # an abandoned Playwright thread must not hang shutdown
