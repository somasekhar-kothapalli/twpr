"""Weekly recorder for the crude EIA report: writes down EVERY Wednesday, traded or not.

    python -m app.crude_recorder                      # the release in data/signal.json -> data/crude_record.json
    python -m app.crude_recorder --date 23-09-2026    # a specific release (Yahoo keeps 1-minute bars ~7 days)
    python -m app.crude_recorder --seed-journal       # copy price paths you already logged with `journal path`
    python -m app.crude_recorder --show               # hit rate, slope and what they say about the anchor

Why: the model's edge is unproven and trades will be rare (about one week in four clears the gate), so waiting for
trades to accumulate would take years. But every Wednesday produces a TLS and a price move, traded or not: about 50
samples a year of "how far does WTI move per mb of surprise, and in which direction". One record per release:
what the signal decided (TLS, Z, demeaned Z, regime), the surprises, the market snapshot and WTI's path from 10:30 ET
to +60 minutes. `--show` then answers: does the surprise's sign predict the direction, how much per mb (against the
0.15-0.30 USD anchor), how much of it is still there when you can enter, and does it hold for the weeks that clear
the gate. It changes nothing in the signal and sends no alert unless it fails.
"""
import argparse
import json
import logging
import os

from dotenv import load_dotenv

from app import journal, model
from app.scraper.utils.calendar import parse_date
from app.surprise_history import load_history
from app.utils.common import (CRUDE_RECORD_FILE, JOURNAL_FILE, MARKET_FILE, ROOT, SIGNAL_FILE,
                              SURPRISE_HISTORY_FILE, fmt_ts, merge_records, now_ist, setup_logging, write_json)
from app.utils.telegram import send_exception

logger = logging.getLogger("twpr.crude_recorder")

ANCHOR_USD_PER_MB = (0.15, 0.30)     # the runbook anchor the message leads with
GATE_Z = 1.25
SHOW_OFFSETS = ("2", "5", "15", "30", "60")   # minutes after the print
MAX_SIGNAL_AGE_DAYS = 2                       # a signal.json older than this is not this week's


def load(path=None):
    path = path or CRUDE_RECORD_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": {}}


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def expected_sign(tls):
    """+1 if the surprise is bullish for price (a draw beyond consensus, TLS below 0), -1 if bearish, None at 0."""
    return None if not tls else (1 if tls < 0 else -1)


def decision_from_signal(signal):
    """The week's decision and inputs from a signal.json dict."""
    calc, sig = signal["calculations"], signal["signal"]
    return {"tls_mb": calc["tls_mb"], "z_tls": calc["z_tls"], "z_tls_demeaned": calc.get("z_tls_demeaned"),
            "sigma_mb": calc["sigma_forecast_mb"], "surprises_mb": {
                "crude": calc["crude_surprise_mb"], "gasoline": calc["gasoline_surprise_mb"],
                "distillate": calc["distillate_surprise_mb"]},
            "cushing_status": calc.get("cushing_status"), "api_surprise_mb": calc.get("api_surprise_mb"),
            "action": sig["action"], "regime": sig["regime"], "direction": sig["direction"],
            "reason": sig.get("reason"), "ovx": (signal.get("inputs") or {}).get("ovx"),
            "atr_20": (signal.get("inputs") or {}).get("atr_20")}


def decision_from_history(row):
    """Only the surprises and TLS, for a week whose signal is gone (the history row is enough for the slope)."""
    tls = model.history_tls(row)
    return {"tls_mb": round(tls, 3), "surprises_mb": {"crude": row["crude_surprise_mb"],
                                                      "gasoline": row["gasoline_surprise_mb"],
                                                      "distillate": row["distillate_surprise_mb"]}}


def aligned(moves, sign):
    """Each move multiplied by the expected direction: positive = WTI moved the way the surprise says."""
    if sign is None or not moves:
        return None
    return {m: None if v is None else round(v * sign, 3) for m, v in moves.items()}


def build_record(release_date, decision, path, market, recorded_at):
    sign = expected_sign((decision or {}).get("tls_mb"))
    return {"release_date": release_date, "recorded_at": recorded_at, "decision": decision,
            "expected_direction": None if sign is None else ("up" if sign > 0 else "down"),
            "market": market, "path": path, "aligned_moves": aligned((path or {}).get("moves"), sign)}


def market_snapshot(release_date, market=None):
    """The afternoon market inputs the signal used (only if market.json is for this release week)."""
    try:
        age = (parse_date(release_date) - parse_date((market or {})["fetched_at"][:10])).days
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= age <= MAX_SIGNAL_AGE_DAYS:      # market.json is from another week
        return None
    return {k: market.get(k) for k in ("wti", "atr_20", "atr_1m", "ovx", "overnight_rally_usd", "cl1_cl2",
                                       "crack_321", "fetched_at")}


def record(release_date, decision, path, market, data=None, recorded_at=None):
    """Merge one release into `data` (the file's content) and return the record."""
    data = data if data is not None else load()
    new = build_record(release_date, decision, path, market_snapshot(release_date, market),
                       recorded_at or fmt_ts(now_ist()))
    data.setdefault("records", {})[release_date] = merge_records(data["records"].get(release_date), new)
    return data["records"][release_date]


def _stats(records, offset, min_abs_z=None):
    """(n, hit rate, mean aligned move, slope USD per mb of |TLS| in the expected direction) at one offset."""
    pts = []
    for rec in records.values():
        move = ((rec.get("path") or {}).get("moves") or {}).get(offset)
        tls = (rec.get("decision") or {}).get("tls_mb")
        z = (rec.get("decision") or {}).get("z_tls")
        sign = expected_sign(tls)
        if move is None or sign is None or (min_abs_z is not None and (z is None or abs(z) < min_abs_z)):
            continue
        pts.append((abs(tls), move * sign))
    if not pts:
        return None
    n = len(pts)
    slope = sum(x * y for x, y in pts) / sum(x * x for x, _ in pts)
    return {"n": n, "hit_rate": sum(y > 0 for _, y in pts) / n, "mean_aligned": sum(y for _, y in pts) / n,
            "slope": slope}


def show(data):
    """Text lines: one row per week, then the statistics for all weeks and for weeks that clear the gate."""
    records = data.get("records", {})
    if not records:
        return ["nothing recorded yet"]
    lines = []
    for date, rec in sorted(records.items(), key=lambda kv: parse_date(kv[0])):
        d, a = rec.get("decision") or {}, rec.get("aligned_moves") or {}
        lines.append(f"{date}  TLS {d.get('tls_mb')}  Z {d.get('z_tls')}  {d.get('action', '?')}  expect "
                     f"{rec.get('expected_direction')}  aligned move: " + "  ".join(
                         f"{m}m {a[m]:+.2f}" for m in SHOW_OFFSETS if a.get(m) is not None))
    for label, gate in (("all weeks", None), (f"weeks with |Z| >= {GATE_Z}", GATE_Z)):
        lines.append(f"-- {label} (aligned move: positive = WTI moved the way the surprise says)")
        for m in SHOW_OFFSETS:
            s = _stats(records, m, gate)
            if s:
                lines.append(f"  +{m:>2} min: n={s['n']:>2}  hit rate {s['hit_rate']:.0%}  mean {s['mean_aligned']:+.2f} USD  "
                             f"slope {s['slope']:+.3f} USD per mb (anchor {ANCHOR_USD_PER_MB[0]}-{ANCHOR_USD_PER_MB[1]})")
    lines.append("A hit rate near 50% or a slope near 0 means the surprise is not predicting direction at that delay. "
                 "Fewer than about 20 weeks is not evidence either way.")
    return lines


def seed_from_journal(data, journal_data):
    """Copy price paths already logged with `journal path`, with the decision snapshot it stored."""
    count = 0
    for date, entry in (journal_data.get("paths") or {}).items():
        snap = entry.get("signal") or {}
        decision = {"tls_mb": snap.get("tls_mb"), "z_tls": snap.get("z_tls"), "action": "trade" if snap.get("regime")
                    else "stand_down", "regime": snap.get("regime"), "direction": snap.get("direction")}
        if snap.get("tls_mb") is None:
            continue
        record(date, decision, entry["path"], None, data, recorded_at=entry.get("logged_at"))
        count += 1
    return count


def main(argv=None, fetch=journal.fetch_print_bars):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Record a crude EIA release: decision and post-print price path")
    parser.add_argument("--date", help="release date DD-MM-YYYY (default: the release in the signal file)")
    parser.add_argument("--seed-journal", action="store_true", help="copy paths logged with `journal path`")
    parser.add_argument("--show", action="store_true", help="print what is recorded and exit")
    args = parser.parse_args(argv)
    if args.show:
        print("\n".join(show(load())), flush=True)   # flush: __main__ ends with os._exit
        return 0
    try:
        data = load()
        if args.seed_journal:
            count = seed_from_journal(data, journal.load(JOURNAL_FILE))
            write_json(CRUDE_RECORD_FILE, data)
            logger.info("seeded %d price paths from the journal", count)
            return 0
        signal = _read(SIGNAL_FILE)
        release_date = args.date or (signal or {}).get("release_date")
        if not release_date:
            raise RuntimeError("no release to record: the signal file is missing and no --date was given")
        age = (now_ist().date() - parse_date(release_date)).days
        if not args.date and age > MAX_SIGNAL_AGE_DAYS:
            logger.info("the signal file is for %s (%d days old): nothing new to record", release_date, age)
            return 0     # a Wednesday-to-Friday schedule finds nothing new on most runs: not a failure
        if signal and signal.get("release_date") == release_date:
            decision = decision_from_signal(signal)
        else:
            rows = [r for r in load_history(SURPRISE_HISTORY_FILE) if r["release_date"] == release_date]
            decision = decision_from_history(rows[0]) if rows else None
        market = _read(MARKET_FILE)
        path = journal.path_from_bars(fetch(release_date), release_date)
        if path is None:
            raise RuntimeError(f"no WTI price just before the {release_date} print in the bars returned")
        rec = record(release_date, decision, path, market, data)
        write_json(CRUDE_RECORD_FILE, data)
        logger.info("recorded %s: TLS %s, %s | moves %s", release_date, (decision or {}).get("tls_mb"),
                    (decision or {}).get("action"), path["moves"])
        return 0
    except Exception as exc:  # noqa: BLE001 - alert, then fail loudly
        logger.exception("crude_recorder failed")
        send_exception("crude_recorder.py", exc)
        return 1


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    os._exit(code)
