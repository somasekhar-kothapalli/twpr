"""Local APScheduler pipeline for the TWPR week — the alternative to GitHub Actions.

    python app/scheduler.py          # run forever
    python app/scheduler.py --test   # run market_data once, then exit

All times are IST (Asia/Kolkata):
    Mon-Fri 09:00  market_data.py
    Tuesday 19:00  consensus_fetcher.py
    Wednesday 01:45  api_monitor.py      (polls)
    Wednesday 19:30  telegram pre-brief
    Wednesday 20:00  eia_parser.py       (polls, then signals and alerts)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from common import IST, now_ist, setup_logging
from telegram_bot import send_message

load_dotenv()

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent

# (job id, script + args, cron kwargs)
JOBS: list[tuple[str, list[str], dict]] = [
    ("market_data", ["market_data.py"], {"day_of_week": "mon-fri", "hour": 9, "minute": 0}),
    ("consensus", ["consensus_fetcher.py", "--no-prompt"], {"day_of_week": "tue", "hour": 19, "minute": 0}),
    ("api_monitor", ["api_monitor.py", "--no-prompt"], {"day_of_week": "wed", "hour": 1, "minute": 45}),
    ("prebrief", ["telegram_bot.py", "--prebrief"], {"day_of_week": "wed", "hour": 19, "minute": 30}),
    ("eia_signal", ["eia_parser.py"], {"day_of_week": "wed", "hour": 20, "minute": 0}),
]

# eia_signal is the only chain: actuals, then the signal, then the alert.
CHAINED_AFTER = {"eia_signal": [["signal_engine.py"], ["telegram_bot.py"]]}


def run_script(args: list[str]) -> int:
    """Run a script as a subprocess, logging its runtime and exit code."""
    command = [sys.executable, str(APP_DIR / args[0]), *args[1:]]
    logger.info("START %s", " ".join(args))
    started = time.monotonic()
    completed = subprocess.run(command, cwd=APP_DIR)
    elapsed = time.monotonic() - started
    logger.info("END   %s | exit %d | %.1fs", " ".join(args), completed.returncode, elapsed)
    return completed.returncode


def run_job(job_id: str, args: list[str]) -> None:
    """Run one scheduled job plus its chain. Alerts on failure; never raises."""
    try:
        code = run_script(args)
        if code != 0:
            send_message(f"⚠️ TWPR scheduler\n{args[0]} exited {code}")
            # A failed step means the chain has nothing valid to work from.
            return

        for chained in CHAINED_AFTER.get(job_id, []):
            code = run_script(chained)
            if code != 0:
                send_message(f"⚠️ TWPR scheduler\n{chained[0]} exited {code}")
                return
    except Exception as exc:  # noqa: BLE001 — the scheduler must survive any job
        logger.exception("Job %s crashed", job_id)
        send_message(f"⚠️ TWPR scheduler\n{job_id} crashed: {exc}")


def build_scheduler() -> BlockingScheduler:
    """Register every TWPR job on an IST scheduler."""
    scheduler = BlockingScheduler(timezone=IST)
    for job_id, args, cron in JOBS:
        scheduler.add_job(
            run_job,
            trigger=CronTrigger(timezone=IST, **cron),
            args=[job_id, args],
            id=job_id,
            name=" ".join(args),
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
        )
    return scheduler


def next_runs(count: int = 5) -> list[tuple[object, str]]:
    """The next `count` fire times across all jobs, soonest first.

    Computed straight off the triggers so it works before the scheduler starts
    (BlockingScheduler.start() blocks, so there is no "after" to print in).
    """
    now = now_ist()
    upcoming = []
    for _job_id, args, cron in JOBS:
        fire_time = CronTrigger(timezone=IST, **cron).get_next_fire_time(None, now)
        if fire_time:
            upcoming.append((fire_time, " ".join(args)))
    return sorted(upcoming, key=lambda pair: pair[0])[:count]


def print_next_runs(count: int = 5) -> None:
    """Print the next scheduled runs in IST."""
    upcoming = next_runs(count)
    print(f"\nNext {len(upcoming)} scheduled runs (IST):")
    for run_time, name in upcoming:
        print(f"  {run_time:%a %Y-%m-%d %H:%M %Z}  {name}")
    print()


def main() -> int:
    """Start the scheduler, or run market_data once with --test."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR local scheduler")
    parser.add_argument("--test", action="store_true", help="run market_data.py now and exit")
    parser.add_argument("--next", action="store_true", help="print the next 5 runs and exit")
    args = parser.parse_args()

    if args.test:
        return run_script(["market_data.py"])
    if args.next:
        print_next_runs()
        return 0

    scheduler = build_scheduler()
    print_next_runs()
    logger.info("TWPR scheduler running (Asia/Kolkata) — Ctrl+C to stop")
    try:
        scheduler.start()  # blocks until interrupted
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
        scheduler.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
