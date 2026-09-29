"""Shared helpers for the pipeline scripts."""
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(__file__).parent.parent.parent / "data"  # <repo>/data (this file is app/utils/common.py)
IST = ZoneInfo("Asia/Kolkata")


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
