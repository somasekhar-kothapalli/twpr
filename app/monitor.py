"""Post-signal exit monitor for the live TWPR trade.

Starts after telegram_bot.py sends the alert. Polls every 60 seconds, enforces
the hard close at 22:30 IST, and alerts on every exit event.

Premium is entered manually for now.
TODO: replace `read_current_premium` with Angel One SmartAPI quotes
(ANGEL_API_KEY / ANGEL_CLIENT_ID / ANGEL_TOTP_SECRET are already reserved in .env).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import time as dtime

from dotenv import load_dotenv

from common import DATA_DIR, now_ist, now_utc, read_json, setup_logging, write_json
from petrocore_client import PetroCoreClient
from telegram_bot import send_error, send_message

load_dotenv()

logger = logging.getLogger(__name__)

SIGNAL_FILE = DATA_DIR / "signal.json"
ACTIVE_TRADE_FILE = DATA_DIR / "active_trade.json"

LOT_SIZE = 100  # barrels per MCX CrudeOil lot
POLL_INTERVAL_SECONDS = 60
HARD_CLOSE_IST = dtime(22, 30)

STOP_LOSS_PCT = -40.0
TARGET_1_PCT = 50.0
TARGET_2_PCT = 100.0


def _prompt_float(label: str, minimum: float | None = None) -> float:
    """Read a float from the terminal, re-asking until it is valid."""
    while True:
        raw = input(f"  {label}: ").strip()
        try:
            value = float(raw)
        except ValueError:
            print("    Not a number — try again")
            continue
        if minimum is not None and value < minimum:
            print(f"    Must be at least {minimum}")
            continue
        return value


def confirm_entry(signal: dict) -> dict | None:
    """Ask whether the trade was taken and collect the fill. None means no trade."""
    print(f"\nTWPR signal: Grade {signal['grade']} {signal['direction']}")
    print(f"  Recommended: {signal['option_type'].upper()} {signal['strike_type']}")
    print(f"  Size: {signal['size_pct']}% of capital | Confidence: {signal['confidence']}")

    if input("\nDid you enter this trade? (y/n): ").strip().lower() != "y":
        return None

    print("\nEnter your fill:")
    lots = int(_prompt_float("Lots", minimum=1))
    entry_premium = _prompt_float("Entry premium (INR)", minimum=0.01)
    option_type = input(f"  Option type [{signal['option_type']}]: ").strip().lower() or signal[
        "option_type"
    ]
    strike = int(_prompt_float("Strike"))

    return {
        "week_ending": signal["week_ending"],
        "setup": "TWPR",
        "trade_type": signal.get("trade_type", "paper"),
        "grade": signal["grade"],
        "direction": signal["direction"],
        "confidence": signal["confidence"],
        "crude_deviation_mb": signal.get("crude_deviation_mb"),
        "option_type": option_type,
        "strike": strike,
        "lots": lots,
        "lots_open": lots,
        "entry_premium": entry_premium,
        "entry_time": now_ist().isoformat(),
        "capital_deployed": entry_premium * lots * LOT_SIZE,
        "exits": [],
        "status": "open",
    }


def read_current_premium(last: float) -> float:
    """Read the current option premium. Blank input reuses the last value."""
    raw = input(f"  Current premium [{last}]: ").strip()
    if not raw:
        return last
    try:
        return float(raw)
    except ValueError:
        logger.warning("Not a number — keeping %s", last)
        return last


def pnl_pct(entry_premium: float, current_premium: float) -> float:
    """Percent change in the option premium."""
    return (current_premium - entry_premium) / entry_premium * 100.0


def check_exit(pct: float, target_1_done: bool, now_time: dtime) -> tuple[str, str] | None:
    """Return (exit_type, scope) when an exit condition fires, else None.

    scope is 'all' or 'half'. Checked stop-first so a whipsaw that trips both
    the stop and the hard close still books as the stop.
    """
    if pct <= STOP_LOSS_PCT:
        return "stop", "all"
    if pct >= TARGET_2_PCT:
        return "target_2", "all"
    if pct >= TARGET_1_PCT and not target_1_done:
        return "target_1", "half"
    if now_time >= HARD_CLOSE_IST:
        return "hard_close", "all"
    return None


def alert_for(exit_type: str, trade: dict, current: float, pct: float) -> str:
    """Build the Telegram text for an exit event."""
    header = {
        "stop": "\U0001f6d1 STOP LOSS HIT",
        "target_1": "\U0001f3af TARGET 1 HIT (+50%)",
        "target_2": "\U0001f3af\U0001f3af TARGET 2 HIT (+100%)",
        "hard_close": "\U0001f3c1 HARD CLOSE — 22:30 IST",
    }[exit_type]

    action = {
        "stop": "Exit all lots now",
        "target_1": "Exit 50% of position now",
        "target_2": "Exit all remaining lots now",
        "hard_close": "Exiting all remaining positions",
    }[exit_type]

    return (
        f"{header}\n"
        f"TWPR | Grade {trade['grade']} {str(trade['direction']).title()}\n"
        f"Entry: ₹{trade['entry_premium']:,.0f} | Current: ₹{current:,.0f}\n"
        f"P&L: {pct:+.1f}% | {action}"
    )


def record_exit(trade: dict, exit_type: str, scope: str, premium: float) -> dict:
    """Append an exit leg to the trade and update how many lots remain open."""
    lots_out = max(1, trade["lots_open"] // 2) if scope == "half" else trade["lots_open"]
    trade["exits"].append(
        {
            "exit_type": exit_type,
            "lots": lots_out,
            "exit_premium": premium,
            "exit_time": now_ist().isoformat(),
            "return_pct": round(pnl_pct(trade["entry_premium"], premium), 2),
        }
    )
    trade["lots_open"] -= lots_out
    if trade["lots_open"] <= 0:
        trade["status"] = "closed"
    write_json(ACTIVE_TRADE_FILE, trade)
    return trade


def monitor_loop(trade: dict, interval: int = POLL_INTERVAL_SECONDS) -> dict:
    """Poll until every lot is out. Returns the closed trade."""
    last_premium = trade["entry_premium"]
    target_1_done = False

    while trade["status"] == "open":
        current = read_current_premium(last_premium)
        last_premium = current
        pct = pnl_pct(trade["entry_premium"], current)
        logger.info(
            "Premium ₹%.1f | P&L %+.1f%% | %d lots open | %s IST",
            current, pct, trade["lots_open"], now_ist().strftime("%H:%M:%S"),
        )

        event = check_exit(pct, target_1_done, now_ist().time())
        if event is None:
            time.sleep(interval)
            continue

        exit_type, scope = event
        send_message(alert_for(exit_type, trade, current, pct))

        confirmed = _prompt_float(f"Actual fill for the {exit_type} exit (INR)", minimum=0.01)
        trade = record_exit(trade, exit_type, scope, confirmed)

        if exit_type == "target_1":
            target_1_done = True
            logger.info("Target 1 booked — %d lots still open", trade["lots_open"])
            if trade["status"] == "open":
                time.sleep(interval)

    return trade


def summarize(trade: dict) -> str:
    """Build the closing Telegram summary."""
    lines = [
        "\U0001f4dd TWPR TRADE CLOSED",
        f"Grade {trade['grade']} {str(trade['direction']).title()} | "
        f"{str(trade['option_type']).upper()} {trade['strike']}",
        f"Entry: ₹{trade['entry_premium']:,.0f} × {trade['lots']} lots",
        "",
    ]
    gross = 0.0
    for leg in trade["exits"]:
        leg_gross = (leg["exit_premium"] - trade["entry_premium"]) * leg["lots"] * LOT_SIZE
        gross += leg_gross
        lines.append(
            f"{leg['exit_type']}: {leg['lots']} lots @ ₹{leg['exit_premium']:,.0f} "
            f"({leg['return_pct']:+.1f}%) = ₹{leg_gross:+,.0f}"
        )
    lines += ["", f"Gross P&L: ₹{gross:+,.0f}", "Log it: python app/journal.py log"]
    return "\n".join(lines)


def main() -> int:
    """Confirm the entry, then monitor until the position is flat."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR post-signal exit monitor")
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL_SECONDS, help="poll seconds (default 60)"
    )
    parser.add_argument("--resume", action="store_true", help="resume data/active_trade.json")
    args = parser.parse_args()

    try:
        if args.resume:
            trade = read_json(ACTIVE_TRADE_FILE)
            if not trade or trade.get("status") != "open":
                raise RuntimeError("no open trade in data/active_trade.json to resume")
        else:
            signal = read_json(SIGNAL_FILE)
            if signal is None:
                raise FileNotFoundError(f"{SIGNAL_FILE} not found — run signal_engine.py first")

            if signal.get("grade") == "skip":
                logger.info("Signal is skip — nothing to monitor")
                return 0

            trade = confirm_entry(signal)
            if trade is None:
                write_json(
                    ACTIVE_TRADE_FILE,
                    {
                        "week_ending": signal["week_ending"],
                        "setup": "TWPR",
                        "grade": signal["grade"],
                        "direction": signal["direction"],
                        "status": "no_trade",
                        "logged_at": now_utc().isoformat(),
                    },
                )
                logger.info("Recorded as no_trade")
                return 0

            write_json(ACTIVE_TRADE_FILE, trade)

        trade = monitor_loop(trade, args.interval)
        send_message(summarize(trade))
        PetroCoreClient().post_trade(trade)
        return 0
    except KeyboardInterrupt:
        logger.warning("Interrupted — data/active_trade.json keeps the current state")
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("monitor.py failed")
        send_error("monitor.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
