"""Telegram alerts. Never raises: an alert failing must not mask the real error."""
import logging

import httpx

from app.utils.common import env

logger = logging.getLogger("twpr.telegram")

TIMEOUT_S = 10.0
MAX_ALERT_CHARS = 1500


def send_message(text):
    """Send plain text (no parse_mode, so figures and underscores need no escaping).
    Returns False - and logs the message instead - if unconfigured or on any failure."""
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - alert not sent:\n%s", text)
        return False
    try:
        response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text}, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.error("Telegram send failed: %s", type(exc).__name__)  # not str(exc): it can hold the URL/token
        return False
    if response.status_code >= 300:
        logger.error("Telegram rejected the alert: HTTP %d", response.status_code)
        return False
    return True


def send_error(script, message):
    return send_message(f"\u26a0\ufe0f TWPR ERROR\n{script}\n{message}")


def send_exception(script, exc):
    """Alert that `script` failed. RuntimeErrors from the race carry the per-site
    reasons, so the message text is the useful part; cut to keep it readable."""
    return send_error(script, f"{type(exc).__name__}: {exc}"[:MAX_ALERT_CHARS])
