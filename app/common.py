"""Shared helpers: paths, logging, IST clock, JSON read/write."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def setup_logging() -> None:
    """Configure root logging in the house format. Safe to call more than once."""
    # Alerts carry the rupee sign and emoji; a cp1252 Windows console would
    # otherwise raise UnicodeEncodeError mid-trade.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def now_utc() -> datetime:
    """Current time, UTC-aware."""
    return datetime.now(timezone.utc)


def now_ist() -> datetime:
    """Current time, IST-aware."""
    return datetime.now(IST)


def week_ending(reference: date | None = None) -> str:
    """EIA report week ending (the most recent Friday on or before `reference`).

    EIA WPSR covers the week ending the Friday before the Wednesday release.
    """
    day = reference or now_ist().date()
    # Monday=0 ... Friday=4. Step back to the latest Friday on or before `day`.
    offset = (day.weekday() - 4) % 7
    return (day - timedelta(days=offset)).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from path, returning `default` when the file is absent."""
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    """Write JSON to path, pretty-printed, UTF-8."""
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
