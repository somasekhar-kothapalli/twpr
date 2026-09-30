"""One command per phase of release day, so the six scripts run in the right order and a failure stops the chain.

    python -m app.run pre                      # afternoon: consensus, API report, market data, then the pre-brief
    python -m app.run print                    # a few minutes before the print: the EIA actuals (polls), the signal, the alert
    python -m app.run print --watch            # ...and then stay on for the time-stop and hard-exit reminders
    python -m app.run all --replay 23-09-2026  # a past release, end to end (replay-stamped; the pre-brief is printed, not sent)
    python -m app.run pre --dry-run            # show the commands, run nothing

Each stage is its own `python -m app.<script>` process (the scrapers open a real browser and end with a hard exit,
so they cannot share a process). The stages already alert Telegram when they fail; this also alerts if one exits
non-zero, and does not start the ones after it: a signal built on stale files is worse than no signal.

Scheduling (Windows Task Scheduler or cron) is just these commands at fixed times; the browser is headed, so
the machine needs a logged-in desktop session.
"""
import argparse
import logging
import os
import subprocess
import sys
from datetime import timedelta

from dotenv import load_dotenv

from app.utils.common import DATE_FORMAT, ROOT, parse_release_date, setup_logging
from app.utils.telegram import send_error

logger = logging.getLogger("twpr.run")


def plan(phase, replay=None):
    """[(module, argv)] for `phase` ('pre', 'print' or 'all'); `replay` is a DD-MM-YYYY release to replay."""
    if replay:
        parse_release_date(replay)
        tuesday = (parse_release_date(replay) - timedelta(days=1)).strftime(DATE_FORMAT)
    pre = [
        ("consensus_fetcher", ["--date", replay] if replay else []),
        ("api_monitor", ["--date", tuesday, "--once"] if replay else ["--once"]),
        ("market_data", ["--date", replay] if replay else []),
        ("pre_brief", ["--allow-stale", "--print"] if replay else []),
    ]
    at_print = [
        ("eia_actuals", ["--date", replay, "--once"] if replay else []),
        ("signal_engine", ["--allow-stale"] if replay else []),
        ("telegram_bot", ["--allow-stale"] if replay else []),
    ]
    return {"pre": pre, "print": at_print, "all": pre + at_print}[phase]


def run_stages(stages, runner=subprocess.run, dry_run=False, alert=send_error):
    """Run each stage in order; stop at the first failure. Returns the exit code (0 = every stage passed)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for module, argv in stages:
        command = [sys.executable, "-m", f"app.{module}", *argv]
        logger.info("stage %s: %s", module, " ".join(["python", "-m", f"app.{module}", *argv]))
        if dry_run:
            continue
        code = runner(command, cwd=ROOT, env=env).returncode
        if code != 0:
            remaining = [m for m, _ in stages[[m for m, _ in stages].index(module) + 1:]]
            logger.error("stage %s failed (exit %d); not running: %s", module, code, ", ".join(remaining) or "nothing")
            alert("run.py", f"stage {module} exited {code}; skipped: {', '.join(remaining) or 'nothing'}")
            return code
    return 0


def main(argv=None, runner=subprocess.run):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run a phase of release day")
    parser.add_argument("phase", choices=("pre", "print", "all"))
    parser.add_argument("--replay", metavar="DD-MM-YYYY", help="replay that release end to end")
    parser.add_argument("--watch", action="store_true", help="after the alert, stay on for the time-stop/hard-exit reminders")
    parser.add_argument("--dry-run", action="store_true", help="show the commands, run nothing")
    args = parser.parse_args(argv)
    try:
        stages = plan(args.phase, args.replay)
    except ValueError:
        parser.error(f"--replay must be a DD-MM-YYYY date, got {args.replay!r}")
    if args.watch and args.phase != "pre":
        stages = stages + [("watch", ["--allow-stale"] if args.replay else [])]
    return run_stages(stages, runner=runner, dry_run=args.dry_run)


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    os._exit(code)
