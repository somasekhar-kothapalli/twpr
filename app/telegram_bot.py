"""Send data/signal.json to Telegram - the success alert. (Errors are alerted by each script.)

Runs right after signal_engine.py. A stale signal.json must never go out as if it
were live: if the engine failed, the file still holds LAST week's signal, so a
signal older than STALE_DAYS is refused (error alert, exit 1). --allow-stale sends
it anyway, stamped as a replay, for testing.

Run from the repo root:
    python -m app.telegram_bot
    python -m app.telegram_bot --allow-stale     # replay; the message says so
"""
import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from app.utils.common import ROOT, SIGNAL_FILE, is_stale, now_ist, parse_release_date, setup_logging
from app.utils.telegram import send_error, send_message

logger = logging.getLogger("twpr.telegram_bot")

STALE_DAYS = 2
BULLISH, BEARISH, NEUTRAL = "\U0001F7E2", "\U0001F534", "⚪"


def _mb(value):
    return f"{value:+.3f} mb" if value is not None else "N/A"


def _status(flag, yes, no):
    return yes if flag else no if flag is False else "unknown"


def format_signal(signal, replay_days=None):
    """The Wednesday alert text for a signal.json dict. `replay_days` (int) stamps it
    as an old signal being replayed, not a live one."""
    trade, calc, inputs = signal["signal"], signal["calculations"], signal["inputs"]
    move = signal["expected_move"]
    lines = []
    if replay_days is not None:
        lines += [f"\U0001F501 REPLAY - data is {replay_days} days old, NOT a live signal", ""]

    if trade["grade"] == "skip":
        lines += [
            f"{NEUTRAL} TWPR - NO TRADE",
            f"Release {signal['release_date']}",
            "",
            f"Crude deviation: {_mb(calc['crude_deviation_mb'])} (inside the ±1.0 skip zone)",
            "No position this week.",
        ]
    else:
        icon = BULLISH if trade["direction"] == "bullish" else BEARISH
        lines += [
            f"{icon} TWPR SIGNAL - Grade {trade['grade']} {trade['direction'].title()}",
            f"Release {signal['release_date']} (generated {signal['generated_at']} IST)",
            "",
            f"Crude deviation: {_mb(calc['crude_deviation_mb'])} "
            f"(actual {_mb(inputs['crude_change_mb'])} vs consensus {_mb(inputs['crude_consensus_mb'])})",
            f"Cushing: {_mb(inputs['cushing_change_mb'])} "
            f"({_status(calc['cushing_contradicts'], 'contradicts', 'confirms')})",
            f"API crude: {_mb(inputs['api_crude_mb'])} ({_status(calc['api_aligns'], 'aligns', 'contradicts')})",
        ]
        if calc["products_oppose"]:
            lines.append(f"Products strongly oppose: gasoline {_mb(calc['gasoline_deviation_mb'])}, "
                         f"distillate {_mb(calc['distillate_deviation_mb'])}")
        lines += [
            "",
            f"Trade: {trade['option_type']} {trade['strike_type']} | size {trade['size_pct']}% of capital",
            f"Confidence: {trade['confidence']}",
            f"Expected WTI move: {move['wti_low']:+.1f} to {move['wti_high']:+.1f} USD "
            f"= {move['mcx_low']:+,} to {move['mcx_high']:+,} INR on MCX "
            f"(USD/INR {move['usd_inr']:.2f}, {move['usd_inr_source']})",
        ]

    if signal.get("analysis"):
        lines += ["", signal["analysis"]]
    return "\n".join(lines)


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Send the TWPR signal to Telegram")
    parser.add_argument("--allow-stale", action="store_true",
                        help="send a signal older than %d days, stamped as a replay" % STALE_DAYS)
    args = parser.parse_args(argv)

    try:
        if not SIGNAL_FILE.exists():
            raise ValueError(f"{SIGNAL_FILE.name} not found - did signal_engine.py run?")
        signal = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
        release_date = signal["release_date"]
        replay_days = None
        if is_stale(release_date, STALE_DAYS):
            age = (now_ist().date() - parse_release_date(release_date)).days
            if not args.allow_stale:
                raise ValueError(f"{SIGNAL_FILE.name} is for {release_date}, {age} days old - "
                                 "signal_engine.py probably failed this week; not sending a stale signal")
            replay_days = age
        text = format_signal(signal, replay_days)
    except Exception as exc:  # noqa: BLE001 - alert instead of sending nothing
        logger.error("cannot build the alert: %s", exc)
        send_error("telegram_bot.py", f"{type(exc).__name__}: {exc}")
        return 1

    if not send_message(text):
        logger.error("Telegram send failed")  # nothing else to alert through
        return 1
    logger.info("signal for %s sent (%d chars)%s", release_date, len(text), " [replay]" if replay_days else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
