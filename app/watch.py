"""Time reminders while you hold the trade (data/signal.json -> Telegram). Alerts only: it never touches an order.

The runbook's exits are about time and discipline, which is where a fast market costs money, so this reminds
you of them: the entry window, the 35-minute time stop, a 15-minute warning before the hard exit, and the hard
exit itself (an hour before the MCX close). It reads the schedule the signal engine already wrote; it needs no
broker and no prices, so it cannot watch your premium stop (that needs a live option quote).

Start it right after the signal alert and leave it running until the hard exit:
    python -m app.watch                  # remind at each time, sleeping in between
    python -m app.watch --dry-run        # list the reminders, send nothing
    python -m app.watch --allow-stale    # replay an old signal (times already past are skipped)
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

from app.utils.common import IST, ROOT, SIGNAL_FILE, now_ist, parse_release_date, setup_logging
from app.utils.telegram import send_error, send_message

logger = logging.getLogger("twpr.watch")

ENTRY_WINDOW_AFTER_MIN = 2      # the runbook waits out the first two minutes
HARD_EXIT_WARNING_MIN = 15
LATE_GRACE_S = 90               # a reminder more than this past its time is skipped, not sent late


def _at(day, hhmm):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def reminders(signal):
    """[(aware IST datetime, text), ...] in time order for a TRADE signal; [] for a stand-down."""
    trade = signal["signal"]
    if trade["action"] != "trade":
        return []
    day = parse_release_date(signal["release_date"])
    schedule = signal["schedule"]
    release, time_stop = _at(day, schedule["release_ist"]), _at(day, schedule["time_stop_ist"])
    hard_exit, close = _at(day, schedule["hard_exit_ist"]), schedule["session_close_ist"]
    label = f"{trade['option_type']} {trade['strike_type']} (Regime {trade['regime']} {trade['direction']})"
    return [
        (release + timedelta(minutes=ENTRY_WINDOW_AFTER_MIN),
         f"{label}: entry window open. Limit orders only; no retest, no trade. Chop exit: 4 minutes after YOUR "
         "fill with no resolution."),
        (time_stop, f"{label}: 35-MINUTE TIME STOP ({schedule['time_stop_ist']} IST). Close the position if it "
                    "has not worked."),
        (hard_exit - timedelta(minutes=HARD_EXIT_WARNING_MIN),
         f"{label}: hard exit in {HARD_EXIT_WARNING_MIN} minutes ({schedule['hard_exit_ist']} IST). Start closing."),
        (hard_exit, f"{label}: HARD EXIT NOW. Close every position. Never carry it to the next session (MCX "
                    f"closes {close} IST)."),
    ]


def run(items, now=now_ist, sleep=time.sleep, send=send_message, dry_run=False):
    """Send each reminder at its time. Returns how many were sent (or listed, with `dry_run`)."""
    count = 0
    for when, text in items:
        wait = (when - now()).total_seconds()
        if dry_run:
            logger.info("%s IST  %s", when.strftime("%H:%M"), text)
            count += 1
            continue
        if wait < -LATE_GRACE_S:
            logger.info("skipping %s IST reminder: already %d s past", when.strftime("%H:%M"), -wait)
            continue
        if wait > 0:
            sleep(wait)
        if send(f"⏰ {text}"):
            count += 1
        else:
            logger.error("reminder for %s IST was not delivered", when.strftime("%H:%M"))
    return count


def main(argv=None, now=now_ist, sleep=time.sleep):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Remind me of the trade's time rules")
    parser.add_argument("--dry-run", action="store_true", help="list the reminders, send nothing")
    parser.add_argument("--allow-stale", action="store_true", help="accept a signal that is not for today")
    args = parser.parse_args(argv)
    try:
        import json
        if not SIGNAL_FILE.exists():
            raise ValueError(f"{SIGNAL_FILE.name} not found - did signal_engine.py run?")
        signal = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
        if signal["release_date"] != now().strftime("%d-%m-%Y") and not args.allow_stale:
            raise ValueError(f"{SIGNAL_FILE.name} is for {signal['release_date']}, not today: nothing to watch "
                             "(use --allow-stale to replay)")
        items = reminders(signal)
    except Exception as exc:  # noqa: BLE001 - alert instead of watching nothing
        logger.error("cannot start the watch: %s", exc)
        send_error("watch.py", f"{type(exc).__name__}: {exc}")
        return 1
    if not items:
        logger.info("signal for %s is a stand-down: nothing to watch", signal["release_date"])
        return 0
    logger.info("watching %d reminders for %s", len(items), signal["release_date"])
    sent = run(items, now=now, sleep=sleep, send=send_message, dry_run=args.dry_run)   # global looked up at call time
    logger.info("%d reminders %s", sent, "listed" if args.dry_run else "sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
