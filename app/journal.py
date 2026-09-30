"""Trade journal and post-print price log: the record that tells you whether this works. You execute by hand;
this keeps score. Nothing here places or changes an order.

    python -m app.journal fill --contract CRUDEOILM --option PUT --strike 9800 --lots 1 \\
        --entry-premium 1310 --exit-premium 1342 --exit-reason time_stop --intended-premium 1305
    python -m app.journal path [--date 23-09-2026]     # WTI after the print, run within ~7 days of it
    python -m app.journal show                         # trades and the price-path statistics

Two questions it exists to answer (runbook section 6.1): how much do fills cost against the price you meant to
pay (slippage), and how much of the expected move is still there by the time you can enter (the price path
after the print, in the direction of the signal)? Nine weeks of history cannot answer either; this can, one
Wednesday at a time.

Charges are ESTIMATES carried over from the earlier pipeline (0.05% commodities transaction tax on the sold
premium, Rs 20 brokerage a leg): check them against your contract note before believing the net figure.
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

from app.market_data import NEW_YORK, PRINT_ET, price_at
from app.options import CONTRACT_BARRELS
from app.utils.common import DATE_FORMAT, JOURNAL_FILE, ROOT, SIGNAL_FILE, fmt, fmt_ts, now_ist, setup_logging, write_json

logger = logging.getLogger("twpr.journal")

CTT_RATE = 0.0005            # ASSUMPTION: 0.05% of the premium on the sell leg
BROKERAGE_PER_LEG_INR = 20.0  # ASSUMPTION: a flat Rs 20 a leg
EXIT_REASONS = ("stop", "time_stop", "chop", "hard_exit", "scale_out", "target", "manual")
PATH_OFFSETS_MIN = (0, 1, 2, 5, 10, 15, 30, 60)   # minutes after the print (10:30 ET)


def pnl(entry_premium, exit_premium, lots, barrels):
    """Gross, estimated charges, net and return for one long option round trip (premiums in Rs per barrel)."""
    quantity = lots * barrels
    gross = (exit_premium - entry_premium) * quantity
    charges = exit_premium * quantity * CTT_RATE + 2 * BROKERAGE_PER_LEG_INR
    return {"gross_pnl_inr": round(gross, 2), "charges_inr": round(charges, 2), "net_pnl_inr": round(gross - charges, 2),
            "return_pct": round((exit_premium - entry_premium) / entry_premium * 100, 2),
            "capital_deployed_inr": round(entry_premium * quantity, 2)}


def load(path=None):
    path = path or JOURNAL_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"trades": [], "paths": {}}


def signal_snapshot(release_date, path=None):
    """The signal's decision for `release_date`, or None if signal.json is for another week."""
    path = path or SIGNAL_FILE
    try:
        signal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if signal.get("release_date") != release_date:
        return None
    move = signal.get("expected_move") or {}
    return {"regime": signal["signal"]["regime"], "direction": signal["signal"]["direction"],
            "option_type": signal["signal"]["option_type"], "tls_mb": signal["calculations"]["tls_mb"],
            "z_tls": signal["calculations"]["z_tls"], "expected_wti_usd": move.get("wti_usd")}


def record_fill(journal, release_date, contract, option_type, strike, lots, entry_premium, exit_premium, exit_reason,
                entry_time=None, exit_time=None, intended_premium=None, notes="", snapshot=None, logged_at=None):
    """Append one trade to `journal` and return it. Slippage is the entry against the limit you meant to
    get, in rupees for the whole position (positive = you paid more than intended)."""
    if contract not in CONTRACT_BARRELS:
        raise ValueError(f"contract must be one of {tuple(CONTRACT_BARRELS)}")
    if option_type not in ("CALL", "PUT"):
        raise ValueError("option type must be CALL or PUT")
    if exit_reason not in EXIT_REASONS:
        raise ValueError(f"exit reason must be one of {EXIT_REASONS}")
    if lots < 1 or entry_premium <= 0 or exit_premium < 0:
        raise ValueError("lots must be at least 1 and the premiums positive")
    barrels = CONTRACT_BARRELS[contract]
    trade = {"release_date": release_date, "contract": contract, "option_type": option_type, "strike": strike,
             "lots": lots, "entry_premium": entry_premium, "exit_premium": exit_premium, "entry_time": entry_time,
             "exit_time": exit_time, "intended_premium": intended_premium, "exit_reason": exit_reason, "notes": notes,
             **pnl(entry_premium, exit_premium, lots, barrels),
             "slippage_inr": None if intended_premium is None else round((entry_premium - intended_premium) * lots * barrels, 2),
             "signal": snapshot, "logged_at": logged_at or fmt_ts(now_ist())}
    journal.setdefault("trades", []).append(trade)
    return trade


def print_time(release_date):
    """10:30 New York time on the release day (an aware datetime)."""
    day = datetime.strptime(release_date, DATE_FORMAT)
    return datetime(day.year, day.month, day.day, *PRINT_ET, tzinfo=NEW_YORK)


def path_from_bars(bars, release_date, offsets=PATH_OFFSETS_MIN):
    """WTI after the print from 1-minute bars [(aware start, open)]: the price just before it, the price at each
    offset (minutes after 10:30 ET), the move from the pre-print price, and the high/low of the first hour.
    None where a price is missing (a gap in the data)."""
    moment = print_time(release_date)
    before = price_at(bars, moment - timedelta(seconds=1))
    if before is None:
        return None
    prices = {str(m): price_at(bars, moment + timedelta(minutes=m)) for m in offsets}
    window = [p for start, p in bars if moment <= start <= moment + timedelta(hours=1)]
    return {"pre_print": round(before, 2),
            "prices": {m: None if p is None else round(p, 2) for m, p in prices.items()},
            "moves": {m: None if p is None else round(p - before, 2) for m, p in prices.items()},
            "high_1h": round(max(window), 2) if window else None, "low_1h": round(min(window), 2) if window else None}


def fetch_print_bars(release_date):
    """1-minute CL=F bars around the print. Yahoo keeps them for about a week."""
    import yfinance as yf
    day = print_time(release_date).date()
    frame = yf.Ticker("CL=F").history(start=day, end=day + timedelta(days=1), interval="1m", timeout=20).dropna()
    if frame.empty:
        raise RuntimeError(f"no 1-minute WTI bars for {release_date}: Yahoo keeps them for about 7 days, so run "
                           "'journal path' soon after the print")
    return [(ts.to_pydatetime(), float(row.Open)) for ts, row in zip(frame.index, frame.itertuples())]


def summarise(journal):
    """Text lines: the trades, the slippage, and the average move in the signal's direction after the print."""
    trades, paths = journal.get("trades", []), journal.get("paths", {})
    lines = []
    if trades:
        wins = [t for t in trades if t["net_pnl_inr"] > 0]
        slips = [t["slippage_inr"] for t in trades if t["slippage_inr"] is not None]
        lines += [f"Trades: {len(trades)} | wins {len(wins)} ({len(wins) / len(trades):.0%}) | net INR "
                  f"{sum(t['net_pnl_inr'] for t in trades):+,.0f} | average net INR "
                  f"{sum(t['net_pnl_inr'] for t in trades) / len(trades):+,.0f}",
                  "Slippage vs intended (INR, whole position): "
                  + (f"average {sum(slips) / len(slips):+,.0f} over {len(slips)} fills" if slips else "no intended prices logged")]
        for t in trades[-5:]:
            lines.append(f"  {t['release_date']} {t['contract']} {t['option_type']} {t['strike']} x{t['lots']}: "
                         f"{t['entry_premium']} -> {t['exit_premium']} ({t['exit_reason']}) net {t['net_pnl_inr']:+,.0f}")
    else:
        lines.append("Trades: none logged yet")
    directed = []
    for release_date, entry in paths.items():
        direction = (entry.get("signal") or {}).get("direction")
        sign = {"bullish": 1, "bearish": -1}.get(direction)
        if sign and entry.get("path"):
            directed.append({m: None if v is None else sign * v for m, v in entry["path"]["moves"].items()})
    if directed:
        lines.append(f"WTI move in the signal's direction after the print ({len(directed)} traded weeks, USD/bbl):")
        for m in (str(x) for x in PATH_OFFSETS_MIN):
            values = [d[m] for d in directed if d.get(m) is not None]
            if values:
                lines.append(f"  +{m:>2} min: average {sum(values) / len(values):+.2f} (min {min(values):+.2f}, max {max(values):+.2f})")
    elif paths:
        lines.append(f"{len(paths)} price paths logged, none for a traded signal yet")
    return lines


def main(argv=None, fetch=fetch_print_bars):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Trade journal and post-print price log")
    sub = parser.add_subparsers(dest="command", required=True)
    fill = sub.add_parser("fill", help="log one completed trade")
    fill.add_argument("--date", default=None, help="release date DD-MM-YYYY (default: today, IST)")
    fill.add_argument("--contract", required=True, choices=tuple(CONTRACT_BARRELS))
    fill.add_argument("--option", required=True, choices=("CALL", "PUT"))
    fill.add_argument("--strike", required=True, type=int)
    fill.add_argument("--lots", required=True, type=int)
    fill.add_argument("--entry-premium", required=True, type=float, help="Rs per barrel")
    fill.add_argument("--exit-premium", required=True, type=float, help="Rs per barrel")
    fill.add_argument("--exit-reason", required=True, choices=EXIT_REASONS)
    fill.add_argument("--intended-premium", type=float, help="the limit price you meant to pay")
    fill.add_argument("--entry-time", help="HH:MM IST")
    fill.add_argument("--exit-time", help="HH:MM IST")
    fill.add_argument("--notes", default="")
    path = sub.add_parser("path", help="record WTI after the print (within about a week)")
    path.add_argument("--date", default=None)
    sub.add_parser("show", help="trades and price-path statistics")
    args = parser.parse_args(argv)

    journal = load()
    release_date = getattr(args, "date", None) or fmt(now_ist().date())
    try:
        if args.command == "fill":
            trade = record_fill(journal, release_date, args.contract, args.option, args.strike, args.lots,
                                args.entry_premium, args.exit_premium, args.exit_reason, args.entry_time,
                                args.exit_time, args.intended_premium, args.notes, signal_snapshot(release_date))
            write_json(JOURNAL_FILE, journal)
            logger.info("logged: %s %s %s x%d %s -> %s: gross INR %+,.0f, charges (estimate) INR %.0f, net INR %+,.0f%s",
                        trade["contract"], trade["option_type"], trade["strike"], trade["lots"], trade["entry_premium"],
                        trade["exit_premium"], trade["gross_pnl_inr"], trade["charges_inr"], trade["net_pnl_inr"],
                        "" if trade["slippage_inr"] is None else f", slippage INR {trade['slippage_inr']:+,.0f}")
        elif args.command == "path":
            found = path_from_bars(fetch(release_date), release_date)
            if found is None:
                raise RuntimeError("no price just before the print in the bars returned")
            journal.setdefault("paths", {})[release_date] = {"path": found, "signal": signal_snapshot(release_date),
                                                              "logged_at": fmt_ts(now_ist())}
            write_json(JOURNAL_FILE, journal)
            logger.info("price path for %s: pre-print %.2f | " + " | ".join(
                f"+{m}m {v:+.2f}" for m, v in found["moves"].items() if v is not None), release_date, found["pre_print"])
        else:
            for line in summarise(journal):
                logger.info("%s", line)
        return 0
    except (ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
