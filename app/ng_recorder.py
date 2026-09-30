"""Passive recorder for the EIA natural gas storage report (Thursdays 10:30 ET). It records; it never decides.

    python -m app.ng_recorder                       # the latest released report -> data/ng_record.json
    python -m app.ng_recorder --date 24-09-2026     # a specific release
    python -m app.ng_recorder --backfill            # every release investing.com still lists (price bars: last ~55 days)
    python -m app.ng_recorder --show                # surprises against the price moves recorded so far

Why: the storage surprise's effect on price is unproven (an 8-print check in docs/NATURAL_GAS_MCX_FACTS.md found no
support for the assumed 0.003-0.005 USD per Bcf), Yahoo keeps 5-minute bars for only ~60 days and the consensus
history on the sites for ~10 weeks, so the only way to get a real sample is to write each Thursday down as it
happens. One record per release: consensus from both sites (their panels differ), the actual, EIA's storage table
(net change against implied flow, stocks against the 5-year average) and NG=F's path after the print. No signal,
no alert on success, no trade. Sizing, the model and any strategy wait for this sample.
"""
import argparse
import json
import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from app.market_data import NEW_YORK, PRINT_ET
from app.scraper.sources import SOURCE_NAMES, scraper_for, slug_for
from app.scraper.utils.calendar import parse_date, row_for_release
from app.utils.common import DATE_FORMAT, NG_RECORD_FILE, ROOT, fmt_ts, now_ist, setup_logging, write_json
from app.utils.ng_storage import fetch_storage
from app.utils.telegram import send_exception

logger = logging.getLogger("twpr.ng_recorder")

SITES = ("tradingeconomics", "investing")
OFFSETS_MIN = (0, 1, 2, 5, 10, 15, 30, 60)   # minutes after the print
MAX_1M_DAYS = 6      # Yahoo keeps 1-minute bars for about a week
MAX_5M_DAYS = 55     # and 5-minute bars for about 60 days


def load(path=None):
    path = path or NG_RECORD_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": {}}


def merge(old, new):
    """`new` over `old`, but a None (or empty dict) in `new` never erases something already recorded, so a
    later run that got less data than an earlier one cannot make the record worse."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return old if new is None else new
    merged = dict(old)
    for key, value in new.items():
        merged[key] = merge(old.get(key), value) if key in old else value
    return merged


def print_time(release_date):
    day = datetime.strptime(release_date, DATE_FORMAT)
    return datetime(day.year, day.month, day.day, *PRINT_ET, tzinfo=NEW_YORK)


def path_from_bars(bars, release_date, interval_min, offsets=OFFSETS_MIN):
    """Price after the print from bars [(aware start, open, high, low, close)] of `interval_min` minutes.
    pre_print = the close of the last bar before 10:30 ET; the price at an offset = the open of the bar that starts
    exactly then (None if no bar does: 5-minute bars have no 1- or 2-minute price). Moves are from pre_print;
    high/low cover the first hour. None if there is no pre-print bar."""
    moment = print_time(release_date)
    before = [b for b in bars if b[0] < moment and moment - b[0] <= timedelta(minutes=2 * interval_min)]
    if not before:
        return None
    pre = max(before, key=lambda b: b[0])[4]
    opens = {b[0]: b[1] for b in bars}
    prices = {str(m): opens.get(moment + timedelta(minutes=m)) for m in offsets}
    hour = [b for b in bars if moment <= b[0] < moment + timedelta(hours=1)]
    r = lambda v: None if v is None else round(v, 3)  # noqa: E731
    return {"interval_min": interval_min, "pre_print": r(pre),
            "prices": {m: r(p) for m, p in prices.items()},
            "moves": {m: None if p is None else r(p - pre) for m, p in prices.items()},
            "high_1h": r(max(b[2] for b in hour)) if hour else None,
            "low_1h": r(min(b[3] for b in hour)) if hour else None}


def fetch_bars(release_date, today=None):
    """(bars, interval_min) for NG=F around the print, 1-minute if Yahoo still has them, else 5-minute, else
    (None, None) with the reason logged."""
    today = today or now_ist().date()
    day = print_time(release_date).date()
    age = (today - day).days
    interval, minutes = ("1m", 1) if age <= MAX_1M_DAYS else ("5m", 5)
    if age > MAX_5M_DAYS:
        logger.warning("no price bars for %s: %d days old, Yahoo keeps 5-minute bars for ~60 days", release_date, age)
        return None, None
    try:
        import yfinance as yf
        frame = yf.Ticker("NG=F").history(start=day, end=day + timedelta(days=1), interval=interval, timeout=20).dropna()
        if frame.empty:
            raise RuntimeError("no bars returned")
        return [(ts.to_pydatetime().astimezone(NEW_YORK), float(r.Open), float(r.High), float(r.Low), float(r.Close))
                for ts, r in zip(frame.index, frame.itertuples())], minutes
    except Exception as exc:  # noqa: BLE001 - the price path is one part of the record
        logger.warning("NG=F bars for %s unavailable: %s", release_date, type(exc).__name__)
        return None, None


def fetch_site_rows(site, make_scraper=scraper_for):
    """The site's storage calendar rows, or None (logged) if it failed. Reasons only, never page content."""
    try:
        with make_scraper(site) as scraper:
            page = scraper.fetch_page(slug_for("ng_storage", site))
        return (page or {}).get("calendar_rows")
    except Exception as exc:  # noqa: BLE001 - one site failing must not stop the other
        logger.warning("%s storage page failed: %s: %s", site, type(exc).__name__, exc)
        return None


def latest_release(rows_by_site):
    """The newest release date with an actual on any site, or None."""
    dates = [parse_date(r["release_date"]) for rows in rows_by_site.values() for r in rows or []
             if r["actual"] is not None and parse_date(r["release_date"])]
    return max(dates).strftime(DATE_FORMAT) if dates else None


def build_record(release_date, rows_by_site, storage, bars, interval_min, recorded_at):
    """One release's record from what each source returned (any of them may be missing)."""
    row = {site: row_for_release(rows, release_date) for site, rows in rows_by_site.items()}
    row = {site: r for site, r in row.items() if r and r["actual"] is not None}
    actuals = {site: r["actual"] for site, r in row.items()}
    actual = next((actuals[s] for s in SITES if s in actuals), None)
    consensus = {SOURCE_NAMES[s]: r["consensus"] for s, r in row.items() if r["consensus"] is not None}
    previous = next((r["previous"] for s in SITES if (r := row.get(s)) and r["previous"] is not None), None)
    eia = storage if storage and storage.get("release_date") == release_date else None
    return {
        "release_date": release_date, "recorded_at": recorded_at,
        "actual_bcf": actual, "previous_bcf": previous,
        "actual_agrees": None if len(actuals) < 2 else len(set(actuals.values())) == 1,
        "consensus_bcf": consensus,
        "surprise_bcf": {s: round(actual - c, 3) for s, c in consensus.items()} if actual is not None else {},
        "eia": eia,   # None when EIA's file is for another week (it only ever holds the latest)
        "price": path_from_bars(bars, release_date, interval_min) if bars else None,
    }


def record(release_date, rows_by_site, storage, bars, interval_min, data=None, recorded_at=None):
    """Merge one release into `data` (the file's content) and return the record."""
    data = data if data is not None else load()
    new = build_record(release_date, rows_by_site, storage, bars, interval_min, recorded_at or fmt_ts(now_ist()))
    data.setdefault("records", {})[release_date] = merge(data["records"].get(release_date), new)
    return data["records"][release_date]


def show(data):
    """Text lines: each release's surprise and the price moves, then the slope of the 5- and 30-minute move
    on the surprise (through the origin) where there are at least 3 releases with both."""
    lines, pairs = [], {"5": [], "30": []}
    for date, rec in sorted(data.get("records", {}).items(), key=lambda kv: parse_date(kv[0])):
        surprise = next(iter((rec.get("surprise_bcf") or {}).values()), None)
        moves = (rec.get("price") or {}).get("moves") or {}
        eia = rec.get("eia") or {}
        lines.append(f"{date}  actual {rec.get('actual_bcf')}  surprise {surprise}  5m {moves.get('5')}  "
                     f"30m {moves.get('30')}  60m {moves.get('60')}  vs5y {eia.get('pct_vs_five_year_avg')}%"
                     + ("  RECLASSIFIED" if eia.get("reclassified") else ""))
        for k in pairs:
            if surprise is not None and moves.get(k) is not None:
                pairs[k].append((-surprise, moves[k]))
    for k, pts in pairs.items():
        if len(pts) >= 3:
            slope = sum(x * y for x, y in pts) / sum(x * x for x, _ in pts if x) if any(x for x, _ in pts) else 0
            lines.append(f"slope at {k} min: {slope:+.4f} USD per Bcf of bullish surprise (n={len(pts)}; the assumed "
                         "anchor is +0.003 to +0.005, positive = the expected direction)")
    return lines or ["nothing recorded yet"]


def main(argv=None, make_scraper=scraper_for):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Record an EIA natural gas storage release (record only)")
    parser.add_argument("--date", help="release date DD-MM-YYYY (default: the latest released report)")
    parser.add_argument("--backfill", action="store_true", help="every release investing.com still lists")
    parser.add_argument("--show", action="store_true", help="print what is recorded and exit")
    parser.add_argument("--sites", nargs="+", choices=SITES, default=list(SITES))
    args = parser.parse_args(argv)
    if args.show:
        print("\n".join(show(load())), flush=True)   # flush: __main__ ends with os._exit
        return 0
    try:
        sites = ("investing",) if args.backfill else tuple(args.sites)
        rows = {site: fetch_site_rows(site, make_scraper) for site in sites}
        rows = {site: r for site, r in rows.items() if r}
        if not rows:
            raise RuntimeError("no site returned the natural gas storage calendar")
        if args.backfill:
            dates = [r["release_date"] for r in rows["investing"] if r["actual"] is not None]
        else:
            date = args.date or latest_release(rows)
            if not date:
                raise RuntimeError("no released natural gas storage report found")
            dates = [date]
        storage = fetch_storage()
        data = load()
        for date in dates:
            bars, minutes = fetch_bars(date)
            rec = record(date, rows, storage, bars, minutes, data)
            logger.info("recorded %s: actual %s consensus %s price path %s", date, rec["actual_bcf"],
                        rec["consensus_bcf"], "yes" if rec["price"] else "no")
        write_json(NG_RECORD_FILE, data)
        return 0
    except Exception as exc:  # noqa: BLE001 - alert, then fail loudly
        logger.exception("ng_recorder failed")
        send_exception("ng_recorder.py", exc)
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    os._exit(code)
