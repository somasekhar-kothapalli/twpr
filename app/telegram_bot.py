"""Send data/signal.json to Telegram - the success alert. (Errors are alerted by each script.)

Runs right after signal_engine.py. A stale signal.json must never go out as if it
were live: if the engine failed, the file still holds LAST week's signal, so a
signal older than STALE_DAYS is refused (error alert, exit 1). --allow-stale sends
it anyway, stamped as a replay, for testing.

Run from the repo root:
    python -m app.telegram_bot
    python -m app.telegram_bot --allow-stale     # replay; the message says so
"""
import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from app.utils.common import ROOT, SIGNAL_FILE, is_stale, now_ist, parse_release_date, setup_logging
from app.utils.telegram import send_error, send_message

logger = logging.getLogger("twpr.telegram_bot")

STALE_DAYS = 2
BULLISH, BEARISH, NEUTRAL = "🟢", "🔴", "⚪"


def _mb(value):
    return f"{value:+.3f} mb" if value is not None else "N/A"


def _format_sizing(sizing):
    if not sizing:
        return ["Lots: set MCX_CRUDEOIL_LOTS (or MCX_CRUDEOILM_LOTS) in .env to see what your lots risk"]
    lines = []
    for name, block in sizing["contracts"].items():
        risk = " | ".join(f"${stop}: {inr:,}" for stop, inr in block["risk_inr_by_futures_stop_usd"].items())
        lines += [f"{name}: {block['lots']} lots x {block['barrels_per_lot']} bbl | INR lost if the option stop is hit, "
                  "by futures stop:", f"  {risk}"]
    return lines


def format_signal(signal, replay_days=None):
    """The Wednesday alert text for a signal.json dict. `replay_days` (int) stamps it
    as an old signal being replayed, not a live one."""
    trade, calc, inputs = signal["signal"], signal["calculations"], signal["inputs"]
    lines = []
    if replay_days is not None:
        lines += [f"🔁 REPLAY - data is {replay_days} days old, NOT a live signal", ""]

    surprises = (f"crude {calc['crude_surprise_mb']:+.3f} | gasoline {calc['gasoline_surprise_mb']:+.3f} | "
                 f"distillate {calc['distillate_surprise_mb']:+.3f}")
    if trade["action"] == "stand_down":
        lines += [
            f"{NEUTRAL} TWPR - STAND DOWN",
            f"Release {signal['release_date']}",
            "",
            f"TLS {calc['tls_mb']:+.3f} mb, Z {calc['z_tls']:+.2f} (sigma {calc['sigma_forecast_mb']:.3f}): "
            "inside the 1.25 sigma noise band.",
            f"Surprises: {surprises}",
            "No position this week.",
        ]
    else:
        icon = BULLISH if trade["direction"] == "bullish" else BEARISH
        cushing = calc["cushing_status"]
        level = inputs.get("cushing_level_mb")
        option, move, schedule = signal["option"], signal["expected_move"], signal["schedule"]
        lines += [
            f"{icon} TWPR SIGNAL - Regime {trade['regime']} {trade['direction'].title()}: "
            f"{trade['option_type']} {trade['strike_type']}",
            f"Release {signal['release_date']} (generated {signal['generated_at']} IST)",
            "",
            f"TLS {calc['tls_mb']:+.3f} mb | Z {calc['z_tls']:+.2f} (sigma {calc['sigma_forecast_mb']:.3f})",
            f"Surprises: {surprises}",
            f"Cushing: {_mb(inputs['cushing_change_mb'])} ({cushing})"
            + (f", level {level:.1f} mb, x{calc['cushing_multiplier']:.2f}" if level is not None else ", level unknown"),
            f"API crude: {_mb(inputs['api_crude_mb'])} ({'aligns' if calc['api_aligns'] else 'contradicts'})",
            "",
            f"Option: delta {option['delta_low']:.2f}-{option['delta_high']:.2f}"
            + (f" (OVX {option['ovx']:.1f} above 35, deep ITM)" if option["ovx_deepened"] else "")
            + f" | expiry {option['expiry_date']} ({option['days_to_expiry']}d"
            + (", rolled" if option["rolled"] else "")
            + ("" if option["expiry_source"] == "mcx_calendar" else ", ASSUMED - check the chain") + ")",
        ]
        guide = option.get("strike_guide")
        if guide:
            lines.append(f"Strike guide ({signal['signal']['option_type']}, Black-76 estimate, OVX as IV - confirm on your "
                         f"chain): delta {option['delta_low']:.2f} ~ {guide['strike_at_delta_low']:,}, "
                         f"delta {option['delta_high']:.2f} ~ {guide['strike_at_delta_high']:,} | futures ~INR "
                         f"{guide['futures_level_inr']:,} | ATM {guide['atm_strike']:,}")
        rupee = signal.get("currency")
        if rupee:
            trend = rupee["usd_inr_trend_pct"]
            lines.append("Rupee: " + ("USD/INR history unavailable" if trend is None else
                         f"USD/INR {trend:+.2f}% over 5 sessions ({rupee['direction'].replace('_', ' ')}), "
                         f"{rupee['effect']} this trade") + ("" if not rupee["notes"] or trend is None else
                                                             " - " + " ".join(rupee["notes"])))
        if move:
            lines.append(f"Expected WTI move (anchor 0.15-0.30 USD per mb of TLS): {move['anchor_low_usd']:+.2f} to "
                         f"{move['anchor_high_usd']:+.2f} USD = {move['anchor_low_inr']:+,} to {move['anchor_high_inr']:+,} INR "
                         f"on MCX (USD/INR {move['usd_inr']:.2f}, {move['usd_inr_source']})")
            band = move.get("band")
            if band:
                lines.append(f"MCX limit: futures band {band['band_pct']:.0f}% = INR {band['band_inr']:,} "
                             f"(on ~INR {band['futures_price_inr']:,}/bbl); the top of that range is "
                             f"{band['move_pct_of_price']:.1f}% of price = {band['move_share_of_band']:.0%} of the band"
                             + (" - CLOSE to a circuit stop" if band["near_band"] else "")
                             + "; the limit widens to 6% then 9%")
            lines.append(f"Model (beta_vol, unproven): {move['wti_usd']:+.2f} USD = {move['mcx_inr']:+,} INR, "
                         f"{move['per_mb_usd']:.2f} USD per mb" + ("" if move["sanity_ok"] else " - outside the anchor"))
        else:
            lines.append("Targets: chart levels (no modelled move for this regime)")
        lines += _format_sizing(signal["sizing"])
        if not schedule.get("mcx_evening_open", True):
            lines += ["", f"!! MCX EVENING SESSION CLOSED on {signal['release_date']} ({schedule['mcx_closed_reason']}): "
                          "you cannot trade this today"]
        lines += ["",
                  f"IST: print {schedule['release_ist']} | time stop {schedule['time_stop_ist']} | "
                  f"hard exit {schedule['hard_exit_ist']} | close {schedule['session_close_ist']} | "
                  f"chop exit {schedule['chop_exit_min']} min",
                  "", "Checklist:"] + [f"- {item}" for item in signal["checklist"]]

    if signal.get("analysis"):
        lines += ["", signal["analysis"]]
    return "\n".join(lines)


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Send the TWPR signal to Telegram")
    parser.add_argument("--allow-stale", action="store_true",
                        help="send a signal older than %d days, stamped as a replay" % STALE_DAYS)
    args = parser.parse_args(argv)

    try:
        if not SIGNAL_FILE.exists():
            raise ValueError(f"{SIGNAL_FILE.name} not found - did signal_engine.py run?")
        signal = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
        release_date = signal["release_date"]
        replay_days = None
        if is_stale(release_date, STALE_DAYS):
            age = (now_ist().date() - parse_release_date(release_date)).days
            if not args.allow_stale:
                raise ValueError(f"{SIGNAL_FILE.name} is for {release_date}, {age} days old - "
                                 "signal_engine.py probably failed this week; not sending a stale signal")
            replay_days = age
        text = format_signal(signal, replay_days)
    except Exception as exc:  # noqa: BLE001 - alert instead of sending nothing
        logger.error("cannot build the alert: %s", exc)
        send_error("telegram_bot.py", f"{type(exc).__name__}: {exc}")
        return 1

    if not send_message(text):
        logger.error("Telegram send failed")  # nothing else to alert through
        return 1
    logger.info("signal for %s sent (%d chars)%s", release_date, len(text), " [replay]" if replay_days else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
