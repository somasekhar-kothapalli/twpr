"""Shared helpers for the pipeline scripts."""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(__file__).parent.parent.parent / "data"  # <repo>/data (this file is app/utils/common.py)
IST = ZoneInfo("Asia/Kolkata")
ROOT = DATA_DIR.parent

# The pipeline's data contract: each file has ONE producer and is read by the next
# stage. Import these; never re-spell a file name (a rename would silently split them).
CONSENSUS_FILE = DATA_DIR / "consensus.json"      # consensus_fetcher -> signal_engine
API_REPORT_FILE = DATA_DIR / "api_report.json"    # api_monitor       -> signal_engine
EIA_ACTUALS_FILE = DATA_DIR / "eia_actuals.json"  # eia_actuals       -> signal_engine
MARKET_FILE = DATA_DIR / "market.json"            # market_data       -> signal_engine
SURPRISE_HISTORY_FILE = DATA_DIR / "surprise_history.json"  # surprise_history -> signal_engine (sigma_forecast)
SIGNAL_FILE = DATA_DIR / "signal.json"            # signal_engine
JOURNAL_FILE = DATA_DIR / "journal.json"          # journal (your own fills and the post-print price paths)
DATE_FORMAT = "%d-%m-%Y"
TIMESTAMP_FORMAT = "%d-%m-%Y %H:%M"


def setup_logging():
    # Windows consoles default to cp1252 and choke on non-ASCII log text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace") # type: ignore
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx logs every request URL at INFO, and the Telegram Bot API carries the bot
    # token in its path - left on, the token lands in every log (incl. CI run logs).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # yfinance logs an ERROR for every symbol it cannot find (an expired futures contract is
    # normal here). Our callers handle missing data themselves and report it once, clearly.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def now_utc():
    return datetime.now(timezone.utc)


def now_ist():
    return datetime.now(IST)


def write_json(path, payload):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def poll(attempt, once, interval_s, timeout_s, log):
    """Call `attempt()` until it returns. A RuntimeError means "not there yet": it is
    logged and retried every `interval_s` until `timeout_s`, then re-raised.
    With `once`, the first RuntimeError is raised immediately. Other errors propagate."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return attempt()
        except RuntimeError as exc:
            if once or time.monotonic() + interval_s > deadline:
                raise
            log.info("not available yet (%s) - retrying in %ds", exc, interval_s)
            time.sleep(interval_s)


def env(name, default=None):
    """An environment variable, treating a placeholder as unset.

    python-dotenv keeps an inline `# ...` comment as the value when the value is
    empty (`GROQ_API_KEY=   # free at ...` becomes the comment text), which then
    fails as a confusing 401. Blank or `#`-leading values count as unset.
    """
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return default if not value or value.startswith("#") else value


def fmt(day):
    """A date -> DD-MM-YYYY."""
    return day.strftime(DATE_FORMAT)


def fmt_ts(moment):
    """A datetime -> DD-MM-YYYY HH:MM."""
    return moment.strftime(TIMESTAMP_FORMAT)


def parse_release_date(text):
    """DD-MM-YYYY -> date; ValueError if it isn't one."""
    return datetime.strptime(text, DATE_FORMAT).date()


def is_stale(release_date, max_age_days=2, today=None):
    """True if the DD-MM-YYYY `release_date` is more than `max_age_days` before `today`
    (default: today, IST)."""
    today = today or now_ist().date()
    return (today - parse_release_date(release_date)).days > max_age_days
