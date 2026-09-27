"""Telegram delivery for TWPR: signal alerts, pre-briefs, and error alerts.

Also the shared notifier — other scripts import `send_message` / `send_error`.
Messages are sent as plain text (no parse_mode) so premium figures and
underscores never need escaping.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

import httpx
from dotenv import load_dotenv

from common import DATA_DIR, env, now_ist, read_json, setup_logging

load_dotenv()

logger = logging.getLogger(__name__)

SIGNAL_FILE = DATA_DIR / "signal.json"
TIMEOUT_SECONDS = 10.0


def send_message(text: str) -> bool:
    """Send a plain-text Telegram message. Returns False on any failure."""
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — message not sent")
        logger.info("Telegram message (unsent):\n%s", text)
        return False

    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.error("Telegram send failed: %s", exc)
        return False

    if response.status_code >= 300:
        logger.error("Telegram rejected message: %d %s", response.status_code, response.text[:300])
        return False

    logger.info("Telegram message sent (%d chars)", len(text))
    return True


def send_error(script_name: str, exc: BaseException) -> bool:
    """Send a truncated error alert for a failed script."""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return send_message(
        f"⚠️ TWPR ERROR\n{script_name}\n{type(exc).__name__}: {exc}\n\n{detail[-1500:]}"
    )


def _strike_lines(signal: dict) -> list[str]:
    """The INR strike guidance block, empty when there is no market data.

    MCX is quoted in INR, so the strike that is actually at the money depends on
    USD/INR as well as WTI — this is the part you act on at 20:00.
    """
    level = signal.get("mcx_implied_level")
    if not level:
        return ["", "Strike: no market data — check the option chain yourself"]

    wanted = "strike_1_otm" if signal.get("strike_type") == "1-OTM" else "strike_atm"
    strike = signal.get(wanted)

    lines = [
        "",
        f"MCX implied: ₹{level:,.0f}  (WTI × USD/INR {signal.get('usd_inr_close')})",
        f"Strike: {strike:,} {str(signal.get('option_type', '')).upper()}"
        if strike
        else "Strike: unavailable",
    ]

    trend = signal.get("usd_inr_trend_pct")
    effect = signal.get("currency_effect")
    if trend is not None and effect in ("amplifies", "dampens"):
        moved = "weaker" if trend > 0 else "stronger"
        lines.append(f"Rupee: {abs(trend):.2f}% {moved} over 5 sessions — {effect} the move")

    return lines


def format_signal(signal: dict) -> str:
    """Render a signal dict as the Wednesday Telegram alert."""
    if signal.get("grade") == "skip":
        deviation = signal.get("crude_deviation_mb")
        if signal.get("skip_reason") == "expiry":
            # Worth distinguishing: the data said trade, the calendar said no.
            return (
                "⚪ TWPR — NO TRADE (expiry)\n"
                f"Week ending {signal.get('week_ending')}\n"
                f"Crude deviation: {deviation:+.3f} mb — tradeable on the data\n"
                f"But the near-month option expires {signal.get('option_expiry')}, "
                f"{signal.get('days_to_expiry')} day(s) away.\n"
                "Too little life left for a -40%/+50% structure. No position."
            )
        return (
            "⚪ TWPR — NO TRADE\n"
            f"Week ending {signal.get('week_ending')}\n"
            f"Crude deviation: {deviation:+.3f} mb "
            "(inside the ±1.0 skip zone)\n"
            "No position this week."
        )

    emoji = "\U0001f7e2" if signal.get("direction") == "bullish" else "\U0001f534"
    lines = [
        f"{emoji} TWPR SIGNAL — Grade {signal.get('grade')} "
        f"{str(signal.get('direction', '')).title()}",
        f"Week ending {signal.get('week_ending')}",
        "",
        f"Crude deviation: {signal.get('crude_deviation_mb'):+.3f} mb",
        f"Cushing: {signal.get('cushing_mb'):+.3f} mb "
        f"({'confirms' if signal.get('cushing_confirms') else 'contradicts'})",
        f"API crude: {signal.get('api_crude_mb'):+.3f} mb "
        f"({'aligns' if signal.get('api_aligns') else 'contradicts'})",
        "",
        f"Trade: {str(signal.get('option_type', '')).upper()} "
        f"{signal.get('strike_type')} | size {signal.get('size_pct')}% of capital",
        f"Confidence: {signal.get('confidence')}",
        f"Expiry: {signal.get('option_expiry')} "
        f"({signal.get('days_to_expiry')} days)"
        if signal.get("days_to_expiry") is not None
        else "Expiry: unknown — check the chain",
    ]

    lines.extend(_strike_lines(signal))
    lines += ["", "Exits: stop -40% | T1 +50% (half) | T2 +100% | hard close 22:30 IST"]

    drivers = signal.get("key_drivers") or []
    if drivers:
        lines += ["", "Drivers:"] + [f"  • {d}" for d in drivers]

    risks = signal.get("risks") or []
    if risks:
        lines += ["", "Risks:"] + [f"  • {r}" for r in risks]

    return "\n".join(lines)


def send_prebrief() -> bool:
    """Send the Wednesday 19:30 IST heads-up before the EIA release."""
    return send_message(
        "⏰ TWPR PRE-BRIEF\n"
        f"{now_ist():%Y-%m-%d %H:%M} IST\n"
        "EIA WPSR releases at 20:00 IST.\n"
        "Terminal open, capital confirmed, hard close is 22:30 IST."
    )


def main() -> int:
    """Send the signal alert (default) or the pre-brief."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR Telegram notifier")
    parser.add_argument("--prebrief", action="store_true", help="send the pre-release brief")
    parser.add_argument("--test", action="store_true", help="send a connectivity test message")
    args = parser.parse_args()

    try:
        if args.test:
            return 0 if send_message("✅ TWPR Telegram test — bot is wired up.") else 1
        if args.prebrief:
            return 0 if send_prebrief() else 1

        signal = read_json(SIGNAL_FILE)
        if signal is None:
            raise FileNotFoundError(f"{SIGNAL_FILE} not found — run signal_engine.py first")

        return 0 if send_message(format_signal(signal)) else 1
    except Exception as exc:  # noqa: BLE001 — top-level guard, re-reported below
        logger.exception("telegram_bot.py failed")
        send_error("telegram_bot.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
