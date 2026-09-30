"""The pre-brief: one Telegram message before the print (data/*.json -> your chat).

Run it on release day after consensus_fetcher, api_monitor and market_data, and before the print. It turns the
runbook's Tuesday/T-0 checks into a page: what the market expects, what the API said, the volatility and the
option you would be looking at, when everything happens, and **how large the surprise has to be before the
model would trade at all**, so the number is in your head before the print, not after.

Read-only: nothing is written except the Telegram message.

Run from the repo root:
    python -m app.pre_brief                 # send it
    python -m app.pre_brief --print         # show it instead of sending
    python -m app.pre_brief --allow-stale   # replay older files
"""
import argparse
import logging
import sys

from dotenv import load_dotenv

from app import model, options
from app.signal_engine import (SCORECARD_THRESHOLDS, InputError, _load_dated, _num, load_market, load_sigma,
                               sigma_method)
from app.utils.common import (API_REPORT_FILE, CONSENSUS_FILE, DATA_DIR, ROOT, is_stale, parse_release_date,
                              setup_logging)
from app.utils.telegram import send_error, send_message

logger = logging.getLogger("twpr.pre_brief")

STALE_DAYS = 2
API_MAX_LEAD_DAYS = 3


def _signed(value, unit="mb", digits=3):
    return "N/A" if value is None else f"{value:+.{digits}f} {unit}"


def gate_thresholds(consensus, sigma, month):
    """What surprise the model needs. `tls` is the |TLS| that clears the 1.25 sigma gate. `crude_bearish` /
    `crude_bullish` are the crude changes that alone would produce it (gasoline and distillate exactly at
    consensus): a build past the first, or a draw past the second. Products moving in step lower the bar,
    against it raise it."""
    tls = model.Z_MIN * sigma
    crude = consensus["crude_consensus_mb"]
    return {"tls": tls, "crude_bearish": crude + tls, "crude_bullish": crude - tls,
            "omega_gasoline": model.omega_gasoline(month), "omega_distillate": model.omega_distillate(month)}


def build_brief(consensus, api, market, sigma, weeks, method, release_day, evening=(True, None)):
    """The message text. Pure: every input is a value."""
    schedule = options.release_schedule(release_day)
    low, high, deepened = options.target_delta(market["ovx"])
    expiry = options.pick_expiry(release_day)
    gate = gate_thresholds(consensus, sigma, release_day.month)
    api_surprise = api["api_crude_mb"] - consensus["crude_consensus_mb"]
    rally = market.get("overnight_rally_usd")
    lines = [f"\U0001F5D3 TWPR PRE-BRIEF - EIA report {consensus['release_date']}", ""]

    if not evening[0]:
        lines += [f"!! MCX EVENING SESSION CLOSED on {consensus['release_date']} ({evening[1]}): a signal cannot be "
                  "traded that day", ""]
    lines += [
        f"Print {schedule['release_ist']} IST (10:30 ET) | time stop {schedule['time_stop_ist']} | hard exit "
        f"{schedule['hard_exit_ist']} | MCX close {schedule['session_close_ist']}",
        "",
        f"Consensus: crude {_signed(consensus['crude_consensus_mb'])} | gasoline "
        f"{_signed(consensus['gasoline_consensus_mb'])} | distillate {_signed(consensus['distillate_consensus_mb'])}",
        f"API ({api['release_date']}): crude {_signed(api['api_crude_mb'])} = {_signed(api_surprise)} vs consensus"
        + (" - PRE-POSITIONED (beyond 3.0 mb)" if abs(api_surprise) > model.API_PREPOSITIONED_MB else ""),
        "",
        f"To trade, |TLS| must reach {gate['tls']:.2f} mb (Z 1.25 x sigma {sigma:.2f}, {method}, {weeks} weeks).",
        f"With products at consensus that is a crude build past {gate['crude_bearish']:+.2f} mb or a draw past "
        f"{gate['crude_bullish']:+.2f} mb (gasoline weight {gate['omega_gasoline']:.2f}, distillate "
        f"{gate['omega_distillate']:.2f}; products in step lower the bar).",
        "",
        f"WTI {market['wti']:.2f} | OVX {market['ovx']:.1f} -> delta {low:.2f}-{high:.2f}"
        + (" (above 35: deep ITM)" if deepened else "") + f" | ATR20 {market['atr_20']:.2f}",
        f"Option: expiry {expiry['expiry_date']} ({expiry['days_to_expiry']}d"
        + (", rolled" if expiry["rolled"] else "")
        + ("" if expiry["expiry_source"] == "mcx_calendar" else ", ASSUMED - check the chain") + ")",
    ]
    checks = []
    for label, key, threshold in (("CL1-CL2", "cl1_cl2", SCORECARD_THRESHOLDS["backwardation_cl1_cl2"]),
                                  ("3:2:1 crack", "crack_321", SCORECARD_THRESHOLDS["crack_321"]),
                                  ("Brent-WTI", "brent_wti", SCORECARD_THRESHOLDS["brent_wti"])):
        value = market.get(key)
        checks.append(f"{label} {'N/A' if value is None else f'{value:+.2f}'} (>{threshold}: "
                      f"{'n/a' if value is None else 'yes' if value > threshold else 'no'})")
    lines.append("Scorecard: " + " | ".join(checks))
    lines.append("Overnight rally (API to now): " + ("N/A" if rally is None else f"{rally:+.2f} USD")
                 + " - Regime 3 needs more than +1.00 with an API draw beyond 3.0 mb"
                 + (" - ARMED" if rally is not None and rally > model.RALLY_MIN_USD
                    and api_surprise < -model.API_PREPOSITIONED_MB else ""))
    trend = market.get("usd_inr_trend_pct")
    if trend is not None:
        lines.append(f"USD/INR {trend:+.2f}% over 5 sessions (context only: the onshore FX market is shut during the hold)")
    return "\n".join(lines)


def load_brief_inputs(data_dir=None, today=None, allow_stale=False):
    """(consensus, api, market) dicts, validated the way the engine does, or raise InputError."""
    data_dir = data_dir or DATA_DIR
    consensus = _load_dated(data_dir / CONSENSUS_FILE.name, "consensus")
    api = _load_dated(data_dir / API_REPORT_FILE.name, "API report")
    for label, data, limit in (("consensus", consensus, STALE_DAYS), ("API report", api, STALE_DAYS + API_MAX_LEAD_DAYS)):
        if not allow_stale and is_stale(data["release_date"], limit, today):
            raise InputError("Stale data", f"{label} is for {data['release_date']}, more than {limit} days old "
                             "(use --allow-stale to replay)")
    lead = (parse_release_date(consensus["release_date"]) - parse_release_date(api["release_date"])).days
    if not 0 <= lead <= API_MAX_LEAD_DAYS:
        raise InputError("Release date mismatch", f"API report {api['release_date']} is not the report released "
                         f"0-{API_MAX_LEAD_DAYS} days before consensus {consensus['release_date']}")
    consensus = {**consensus, **{k: _num(consensus, k) for k in ("crude_consensus_mb", "gasoline_consensus_mb",
                                                                  "distillate_consensus_mb")}}
    api = {**api, "api_crude_mb": _num(api, "api_crude_mb")}
    missing = [name for name, value in (("crude_consensus_mb", consensus["crude_consensus_mb"]),
                                        ("api_crude_mb", api["api_crude_mb"])) if value is None]
    if missing:
        raise InputError("Mandatory field missing", ", ".join(missing))
    return consensus, api, load_market(data_dir, today, allow_stale)


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Send the pre-print brief")
    parser.add_argument("--print", action="store_true", dest="show", help="print it instead of sending")
    parser.add_argument("--allow-stale", action="store_true", help="accept files older than %d days (replay)" % STALE_DAYS)
    args = parser.parse_args(argv)
    try:
        consensus, api, market = load_brief_inputs(allow_stale=args.allow_stale)
        method = sigma_method()
        sigma, weeks = load_sigma(consensus["release_date"], method=method)
        release_day = parse_release_date(consensus["release_date"])
        text = build_brief(consensus, api, market, sigma, weeks, method, release_day,
                           evening=options.mcx_evening_session(release_day))
    except InputError as exc:
        logger.error("%s", exc)
        send_error("pre_brief.py", f"{exc.title}\n{exc.detail}")
        return 1
    except Exception as exc:  # noqa: BLE001 - alert instead of sending nothing
        logger.exception("pre_brief failed")
        send_error("pre_brief.py", f"Unexpected {type(exc).__name__}: {exc}")
        return 1
    if args.show:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        print(text)
        return 0
    if not send_message(text):
        logger.error("Telegram send failed")
        return 1
    logger.info("pre-brief for %s sent (%d chars)", consensus["release_date"], len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
