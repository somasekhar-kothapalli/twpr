"""Race several scraping sites at once; the first valid answer per field wins.

A `job(site, stop, ok, fail)` runs on its own daemon thread per site and reports
each field as it lands: `ok(field, candidate)` or `fail(field, exc)`. A candidate
is a dict carrying at least `release_date`. Fields are decided independently, so
different sites can win different fields, but every winner must share one
release date, anchored by the first field (or by `release_date` if given).
"""
import logging
import queue
import threading
import time

from app.scraper.utils.calendar import format_date

logger = logging.getLogger("twpr.racer")


def decide(fields, candidates, release_date=None):
    """First candidate per field, all for one release date.

    `candidates` = {field: [(site, candidate), ...]} in arrival order. The date is
    anchored by `release_date` if given, else by the first field's winner; until
    it is known the other fields stay undecided. Returns {field: (site, candidate)}.
    """
    anchor = format_date(release_date) if release_date else None
    decided = {}
    for field in fields:
        if anchor is None and field != fields[0]:
            break
        for site, cand in candidates.get(field, []):
            if anchor is None or cand["release_date"] == anchor:
                decided[field] = (site, cand)
                anchor = anchor or cand["release_date"]
                break
    return decided


def race(sites, fields, job, release_date=None, timeout_s=480, what="value"):
    """Run `job` for every site at once; return {field: (site, candidate)} for all
    `fields`, or raise RuntimeError naming each field nobody could deliver."""
    out = queue.Queue()
    stop = threading.Event()

    def worker(site):
        try:
            job(site, stop,
                lambda field, cand: out.put((site, field, cand, None)),
                lambda field, exc: out.put((site, field, None, exc)))
        except BaseException as exc:  # noqa: BLE001 - a crashed job loses its site, not the race
            for field in fields:
                out.put((site, field, None, exc))
        finally:
            out.put((site, None, None, None))  # site finished

    for site in sites:
        threading.Thread(target=worker, args=(site,), daemon=True, name=site).start()

    candidates = {f: [] for f in fields}
    errors = {f: {} for f in fields}
    running = len(sites)
    timed_out = False
    deadline = time.monotonic() + timeout_s
    while running:
        try:
            site, field, cand, exc = out.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            timed_out = True
            break
        if field is None:
            running -= 1
            continue
        if exc is not None:
            errors[field][site] = exc
            logger.warning("%s / %s failed: %s", site, field, exc)
            continue
        candidates[field].append((site, cand))
        decided = decide(fields, candidates, release_date)
        if len(decided) == len(fields):
            stop.set()
            return decided

    stop.set()
    decided = decide(fields, candidates, release_date)
    problems = []
    for field in fields:
        if field in decided:
            continue
        why = "; ".join(f"{s}: {e}" for s, e in errors[field].items())
        if candidates[field] and not why:
            why = f"values found but for a different release date than {fields[0]}'s"
        none = f"no answer within {timeout_s}s" if timed_out else "no site supplied it"
        problems.append(f"{field} ({why or none})")
    raise RuntimeError(f"no valid {what} for " + ", ".join(problems))
