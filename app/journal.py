"""TWPR trade journal: log completed trades and report performance.

Usage:
    python app/journal.py log     # log a completed trade interactively
    python app/journal.py week    # summary of the current week
    python app/journal.py month   # summary of the current month
    python app/journal.py stats   # all-time statistics
    python app/journal.py export  # export to CSV
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from common import DATA_DIR, now_ist, read_json, setup_logging, week_ending, write_json
from petrocore_client import PetroCoreClient

load_dotenv()

logger = logging.getLogger(__name__)

JOURNAL_FILE = DATA_DIR / "journal.json"
ACTIVE_TRADE_FILE = DATA_DIR / "active_trade.json"
EXPORT_FILE = DATA_DIR / "journal_export.csv"

LOT_SIZE = 100  # barrels per MCX CrudeOil lot
CTT_RATE = 0.0005  # 0.05% Commodities Transaction Tax on the premium
BROKERAGE_PER_LEG = 20.0  # INR, Zerodha

EXIT_TYPES = ("stop", "target_1", "target_2", "hard_close", "manual")

RULE = "─" * 30
HEADER = "TradeDesk — TWPR Performance"


def calculate_pnl(entry_premium: float, exit_premium: float, lots: int) -> dict:
    """Gross P&L, charges, net P&L and return percent for one round trip."""
    gross_pnl = (exit_premium - entry_premium) * lots * LOT_SIZE
    ctt_charge = entry_premium * lots * LOT_SIZE * CTT_RATE
    brokerage = BROKERAGE_PER_LEG * 2  # entry + exit
    return {
        "gross_pnl": round(gross_pnl, 2),
        "ctt_charge": round(ctt_charge, 2),
        "brokerage": brokerage,
        "net_pnl": round(gross_pnl - ctt_charge - brokerage, 2),
        "return_pct": round((exit_premium - entry_premium) / entry_premium * 100, 2),
        "capital_deployed": round(entry_premium * lots * LOT_SIZE, 2),
    }


def load_trades() -> list[dict]:
    """All logged trades, oldest first."""
    return (read_json(JOURNAL_FILE) or {}).get("trades", [])


def save_trades(trades: list[dict]) -> None:
    """Persist the trade list."""
    write_json(JOURNAL_FILE, {"trades": trades})


def _prompt(label: str, default: object = None, cast=str):
    """Read one value from the terminal, falling back to `default` on blank input."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return cast(raw)
        except ValueError:
            print(f"    Invalid — expected {cast.__name__}")


def log_trade() -> dict:
    """Log one completed trade, pre-filling from data/active_trade.json when present."""
    active = read_json(ACTIVE_TRADE_FILE) or {}
    if active.get("status") == "no_trade":
        active = {}

    # A partially-exited trade has several legs; the journal stores one weighted row.
    legs = active.get("exits") or []
    weighted_exit = (
        sum(leg["exit_premium"] * leg["lots"] for leg in legs) / sum(leg["lots"] for leg in legs)
        if legs
        else None
    )

    print("\nLog a completed trade (blank keeps the bracketed default):")
    trade = {
        "week_ending": _prompt("Week ending", active.get("week_ending") or week_ending()),
        "setup": "TWPR",
        "trade_type": _prompt("Trade type (paper/live)", active.get("trade_type", "paper")),
        "grade": _prompt("Grade (A/B)", active.get("grade")),
        "direction": _prompt("Direction (bullish/bearish)", active.get("direction")),
        "confidence": _prompt("Confidence", active.get("confidence"), int),
        "crude_deviation_mb": _prompt(
            "Crude deviation (mb)", active.get("crude_deviation_mb"), float
        ),
        "option_type": _prompt("Option type (call/put)", active.get("option_type")),
        "strike": _prompt("Strike", active.get("strike"), int),
        "lots": _prompt("Lots", active.get("lots"), int),
        "entry_premium": _prompt("Entry premium", active.get("entry_premium"), float),
        "entry_time": _prompt("Entry time", active.get("entry_time") or now_ist().isoformat()),
        "exit_premium": _prompt(
            "Exit premium", round(weighted_exit, 2) if weighted_exit else None, float
        ),
        "exit_time": _prompt(
            "Exit time", legs[-1]["exit_time"] if legs else now_ist().isoformat()
        ),
        "exit_type": _prompt(
            f"Exit type ({'/'.join(EXIT_TYPES)})",
            legs[-1]["exit_type"] if legs else None,
        ),
    }

    trade.update(
        calculate_pnl(trade["entry_premium"], trade["exit_premium"], trade["lots"])
    )
    trade["notes"] = input("  Notes: ").strip()

    trades = load_trades()
    trade["trade_id"] = max((t.get("trade_id", 0) for t in trades), default=0) + 1
    trades.append(trade)
    save_trades(trades)

    logger.info(
        "Logged trade #%d: %s %s | net ₹%+,.0f (%+.1f%%)",
        trade["trade_id"], trade["grade"], trade["direction"],
        trade["net_pnl"], trade["return_pct"],
    )
    PetroCoreClient().post_trade(trade)
    return trade


def _in_period(trade: dict, start: date, end: date) -> bool:
    """True when the trade's week_ending falls in [start, end]."""
    try:
        week = datetime.fromisoformat(trade["week_ending"]).date()
    except (KeyError, ValueError):
        return False
    return start <= week <= end


def summarize(trades: list[dict], title: str) -> str:
    """Render the performance block for a set of trades."""
    if not trades:
        return f"{HEADER}\n{RULE}\nPeriod:         {title}\nNo trades logged.\n"

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    paper = [t for t in trades if t.get("trade_type") == "paper"]

    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    lines = [
        HEADER,
        RULE,
        f"Period:         {title}",
        f"Trades logged:  {len(trades)}",
        f"Paper trades:   {len(paper)}  ({len(trades) - len(paper)} live)",
        "",
        f"Win rate:       {len(wins) / len(trades) * 100:.1f}%  "
        f"({len(wins)}W / {len(losses)}L)",
    ]

    if wins:
        lines.append(f"Avg winner:     {sum(t['return_pct'] for t in wins) / len(wins):+.1f}%")
    if losses:
        lines.append(f"Avg loser:      {sum(t['return_pct'] for t in losses) / len(losses):+.1f}%")
    lines.append(f"Profit factor:  {profit_factor:.2f}")
    lines.append("")

    for grade in ("A", "B"):
        graded = [t for t in trades if t.get("grade") == grade]
        if not graded:
            continue
        grade_wins = [t for t in graded if t["net_pnl"] > 0]
        lines.append(
            f"Grade {grade}:        {len(graded)} trades | "
            f"{len(grade_wins) / len(graded) * 100:.0f}% win rate | "
            f"avg {sum(t['return_pct'] for t in graded) / len(graded):+.0f}%"
        )

    lines += ["", "Exits by type:"]
    for exit_type in EXIT_TYPES:
        matching = [t for t in trades if t.get("exit_type") == exit_type]
        if matching:
            lines.append(
                f"  {exit_type + ':':<13} {len(matching)} "
                f"({len(matching) / len(trades) * 100:.0f}%)"
            )

    net = sum(t["net_pnl"] for t in trades)
    label = "paper" if len(paper) == len(trades) else "mixed"
    lines += ["", f"Net P&L:        ₹{net:+,.0f}  ({label})"]
    return "\n".join(lines)


def export_csv() -> int:
    """Write every logged trade to data/journal_export.csv. Returns the row count."""
    trades = load_trades()
    if not trades:
        logger.warning("No trades to export")
        return 0

    columns = list(dict.fromkeys(key for trade in trades for key in trade))
    with EXPORT_FILE.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(trades)

    logger.info("Exported %d trades to %s", len(trades), EXPORT_FILE)
    return len(trades)


def main() -> int:
    """Dispatch the journal subcommand."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR trade journal")
    parser.add_argument("command", choices=("log", "week", "month", "stats", "export"))
    args = parser.parse_args()

    try:
        if args.command == "log":
            log_trade()
            return 0
        if args.command == "export":
            return 0 if export_csv() else 1

        today = now_ist().date()
        trades = load_trades()

        if args.command == "week":
            start = today - timedelta(days=today.weekday())
            print(summarize(
                [t for t in trades if _in_period(t, start, today)],
                f"week of {start.isoformat()}",
            ))
        elif args.command == "month":
            start = today.replace(day=1)
            print(summarize(
                [t for t in trades if _in_period(t, start, today)],
                f"{start:%b %Y}",
            ))
        else:
            first = min((t["week_ending"] for t in trades), default=None)
            print(summarize(trades, f"{first} → present" if first else "all time"))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.exception("journal.py failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
