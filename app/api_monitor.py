"""Capture the API (American Petroleum Institute) private inventory report.

Runs Wednesday 01:45 IST and polls until the report lands (API publishes
Tuesday 16:30 ET). Source order matches consensus_fetcher: CLI flags, then a
scraper module if one is present, then the terminal.

The API report only nudges confidence by +/-5, so signal_engine.py treats a
missing api_report.json as 0.0 rather than blocking the week's trade.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

from common import DATA_DIR, now_utc, read_json, setup_logging, week_ending, write_json
from petrocore_client import PetroCoreClient
from telegram_bot import send_error, send_message

load_dotenv()

logger = logging.getLogger(__name__)

API_REPORT_FILE = DATA_DIR / "api_report.json"

FIELDS = {
    "api_crude_mb": "API crude (mb, negative = draw)",
    "api_cushing_mb": "API Cushing (mb)",
    "api_gasoline_mb": "API gasoline (mb)",
    "api_distillate_mb": "API distillate (mb)",
}

POLL_INTERVAL_SECONDS = 300
POLL_TIMEOUT_SECONDS = 4 * 60 * 60


def _from_scraper(module_name: str) -> tuple[dict, str] | None:
    """Try `module_name.fetch_api_report()`. Returns None when absent or failing."""
    try:
        module = __import__(module_name)
    except ImportError:
        logger.info("%s.py not present — skipping", module_name)
        return None

    fetcher = getattr(module, "fetch_api_report", None)
    if fetcher is None:
        logger.warning("%s.py has no fetch_api_report() — skipping", module_name)
        return None

    try:
        data = fetcher()
    except Exception as exc:  # noqa: BLE001 — a broken scraper must not kill the run
        logger.error("%s.fetch_api_report() failed: %s", module_name, exc)
        return None

    if not data:
        logger.info("%s: API report not published yet", module_name)
        return None

    missing = [f for f in FIELDS if f not in data]
    if missing:
        logger.error("%s.fetch_api_report() omitted %s — discarding", module_name, missing)
        return None

    return {f: float(data[f]) for f in FIELDS}, module_name.replace("_scraper", ".com")


def _from_prompt() -> tuple[dict, str]:
    """Ask for the four numbers on the terminal."""
    print("\nEnter the API report figures:")
    values = {}
    for field, label in FIELDS.items():
        while True:
            raw = input(f"  {label}: ").strip()
            try:
                values[field] = float(raw)
                break
            except ValueError:
                print("    Not a number — try again (e.g. 1.25)")
    return values, "manual"


def poll_for_report(args: argparse.Namespace) -> tuple[dict, str]:
    """Resolve the API report, polling the scrapers until the timeout."""
    if args.crude is not None:
        if None in (args.cushing, args.gasoline, args.distillate):
            raise ValueError("--crude requires --cushing, --gasoline and --distillate too")
        return (
            {
                "api_crude_mb": args.crude,
                "api_cushing_mb": args.cushing,
                "api_gasoline_mb": args.gasoline,
                "api_distillate_mb": args.distillate,
            },
            "manual",
        )

    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        for module_name in ("investing_scraper", "tradingeconomics_scraper"):
            result = _from_scraper(module_name)
            if result is not None:
                return result

        if args.once or time.monotonic() >= deadline:
            break
        logger.info("API report not available yet — retrying in %ds", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)

    if args.no_prompt:
        raise RuntimeError(
            "API report unavailable: pass --crude/--cushing/--gasoline/--distillate, "
            "or add app/investing_scraper.py with a fetch_api_report() function"
        )

    return _from_prompt()


def main() -> int:
    """Capture the API report, save it, and publish it to PetroCore."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR API report monitor")
    parser.add_argument("--crude", type=float, help="API crude in mb (negative = draw)")
    parser.add_argument("--cushing", type=float, help="API Cushing in mb")
    parser.add_argument("--gasoline", type=float, help="API gasoline in mb")
    parser.add_argument("--distillate", type=float, help="API distillate in mb")
    parser.add_argument("--once", action="store_true", help="single attempt, no polling")
    parser.add_argument(
        "--no-prompt", action="store_true", help="fail instead of prompting (for CI)"
    )
    parser.add_argument("--week", help="week ending (YYYY-MM-DD)")
    args = parser.parse_args()

    try:
        week = args.week or week_ending()
        existing = read_json(API_REPORT_FILE) or {}
        if existing.get("week_ending") == week:
            logger.info("API report for %s already saved — nothing to do", week)
            return 0

        values, source = poll_for_report(args)
        payload = {
            "week_ending": week,
            "report_date": now_utc().date().isoformat(),
            **values,
            "source": source,
            "released_at": now_utc().isoformat(),
        }
        write_json(API_REPORT_FILE, payload)
        logger.info("API report %s (%s): crude %+.3f mb", week, source, payload["api_crude_mb"])

        send_message(
            f"\U0001f4ca API report ({week})\n"
            f"Crude {payload['api_crude_mb']:+.3f} mb | "
            f"Cushing {payload['api_cushing_mb']:+.3f} mb\n"
            f"Gasoline {payload['api_gasoline_mb']:+.3f} mb | "
            f"Distillate {payload['api_distillate_mb']:+.3f} mb"
        )
        PetroCoreClient().post_api_report(payload)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("api_monitor.py failed")
        send_error("api_monitor.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
