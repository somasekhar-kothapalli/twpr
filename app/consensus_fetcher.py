"""Fetch the Reuters/Bloomberg analyst consensus for this week's EIA report.

Runs Tuesday 19:00 IST. Source order:
  1. CLI flags (--crude/--gasoline/--distillate/--previous) — always wins
  2. app/investing_scraper.py, if present, via `fetch_consensus()`
  3. app/tradingeconomics_scraper.py, if present, via `fetch_consensus()`
  4. interactive prompt (unless --no-prompt)

The consensus is the denominator of every signal, so a wrong or stale number is
worse than no number: this script fails loudly rather than guessing.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from common import DATA_DIR, now_utc, setup_logging, week_ending, write_json
from petrocore_client import PetroCoreClient
from telegram_bot import send_error

load_dotenv()

logger = logging.getLogger(__name__)

CONSENSUS_FILE = DATA_DIR / "consensus.json"

FIELDS = {
    "crude_consensus_mb": "Crude oil consensus (mb, negative = draw)",
    "gasoline_consensus_mb": "Gasoline consensus (mb)",
    "distillate_consensus_mb": "Distillate consensus (mb)",
    "crude_previous_mb": "Previous week's actual crude change (mb)",
}


def _from_scraper(module_name: str) -> tuple[dict, str] | None:
    """Try `module_name.fetch_consensus()`. Returns None when absent or failing."""
    try:
        module = __import__(module_name)
    except ImportError:
        logger.info("%s.py not present — skipping", module_name)
        return None

    fetcher = getattr(module, "fetch_consensus", None)
    if fetcher is None:
        logger.warning("%s.py has no fetch_consensus() — skipping", module_name)
        return None

    try:
        data = fetcher()
    except Exception as exc:  # noqa: BLE001 — a broken scraper must not kill the run
        logger.error("%s.fetch_consensus() failed: %s", module_name, exc)
        return None

    missing = [f for f in FIELDS if f not in data]
    if missing:
        logger.error("%s.fetch_consensus() omitted %s — discarding", module_name, missing)
        return None

    return {f: float(data[f]) for f in FIELDS}, module_name.replace("_scraper", ".com")


def _from_prompt() -> tuple[dict, str]:
    """Ask for the four numbers on the terminal."""
    print("\nEnter this week's consensus (from Reuters/Bloomberg survey):")
    values = {}
    for field, label in FIELDS.items():
        while True:
            raw = input(f"  {label}: ").strip()
            try:
                values[field] = float(raw)
                break
            except ValueError:
                print("    Not a number — try again (e.g. -1.6)")
    return values, "manual"


def fetch_consensus(args: argparse.Namespace) -> tuple[dict, str]:
    """Resolve the consensus from CLI flags, a scraper, or the terminal."""
    if args.crude is not None:
        if None in (args.gasoline, args.distillate, args.previous):
            raise ValueError("--crude requires --gasoline, --distillate and --previous too")
        return (
            {
                "crude_consensus_mb": args.crude,
                "gasoline_consensus_mb": args.gasoline,
                "distillate_consensus_mb": args.distillate,
                "crude_previous_mb": args.previous,
            },
            "manual",
        )

    for module_name in ("investing_scraper", "tradingeconomics_scraper"):
        result = _from_scraper(module_name)
        if result is not None:
            return result

    if args.no_prompt:
        raise RuntimeError(
            "No consensus source available: pass --crude/--gasoline/--distillate/--previous, "
            "or add app/investing_scraper.py with a fetch_consensus() function"
        )

    return _from_prompt()


def main() -> int:
    """Resolve the consensus, save it, and publish it to PetroCore."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR consensus fetcher")
    parser.add_argument("--crude", type=float, help="crude consensus in mb (negative = draw)")
    parser.add_argument("--gasoline", type=float, help="gasoline consensus in mb")
    parser.add_argument("--distillate", type=float, help="distillate consensus in mb")
    parser.add_argument("--previous", type=float, help="previous week's actual crude change in mb")
    parser.add_argument(
        "--no-prompt", action="store_true", help="fail instead of prompting (for CI)"
    )
    parser.add_argument("--week", help="week ending (YYYY-MM-DD); defaults to the Friday the report covers")
    args = parser.parse_args()

    try:
        values, source = fetch_consensus(args)
        payload = {
            "week_ending": args.week or week_ending(),
            **values,
            "source": source,
            "fetched_at": now_utc().isoformat(),
        }
        write_json(CONSENSUS_FILE, payload)
        logger.info(
            "Consensus %s (%s): crude %+.3f | gasoline %+.3f | distillate %+.3f",
            payload["week_ending"],
            source,
            payload["crude_consensus_mb"],
            payload["gasoline_consensus_mb"],
            payload["distillate_consensus_mb"],
        )

        PetroCoreClient().post_consensus(payload)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("consensus_fetcher.py failed")
        send_error("consensus_fetcher.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
