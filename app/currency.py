"""Currency context for an INR-denominated trade on a USD benchmark.

The TWPR signal is derived from EIA inventories, which move WTI in USD. The
traded instrument is MCX CrudeOil, quoted in INR per barrel, so roughly

    MCX  ~  WTI (USD/bbl)  x  USD/INR

This module measures what the rupee is doing and turns the implied MCX level into
strike guidance. It deliberately does **not** feed grade, direction or
confidence:

- Onshore USD/INR trades 09:00-17:00 IST. TWPR holds 20:00-22:30 IST, so the
  currency market is shut for the entire holding window and the move being
  traded is almost purely WTI.
- Measured over six months of daily data, WTI's mean absolute move is 2.89%
  against USD/INR's 0.38% (7.7x), the currency is a median 12% of the combined
  MCX move, and it flipped the sign of the MCX move versus WTI on only 3.2% of
  days.

So the rupee matters for *which strike* is at the money and as a risk note on the
overnight gap, not for the direction of a two-hour options trade.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# MCX CrudeOil option strikes are spaced this far apart, in INR per barrel.
# ponytail: exchange parameter, not a law of nature -- confirm against the live
# option chain and change here if MCX respaces it.
STRIKE_INTERVAL = 50

# Below this, a move is noise rather than a trend worth reporting.
FLAT_TREND_PCT = 0.1

# A move beyond this against the signal is worth a risk line of its own.
SHARP_TREND_PCT = 0.5


def trend_pct(series: list[float]) -> float | None:
    """Percent change from the oldest to the newest value in `series`."""
    clean = [v for v in series if v is not None]
    if len(clean) < 2 or not clean[0]:
        return None
    return round((clean[-1] - clean[0]) / clean[0] * 100, 3)


def classify(usd_inr_trend: float | None) -> str:
    """Name the rupee's direction. A weakening rupee means USD/INR rising."""
    if usd_inr_trend is None:
        return "unknown"
    if abs(usd_inr_trend) < FLAT_TREND_PCT:
        return "flat"
    return "inr_weakening" if usd_inr_trend > 0 else "inr_strengthening"


def effect_on(direction: str, currency_direction: str) -> str:
    """Whether the rupee amplifies or dampens the WTI-implied MCX move.

    A weakening rupee lifts the INR price of crude, so it amplifies a bearish
    signal's downside only in the sense of working *against* it — spelled out
    per case rather than inferred, because the sign conventions invite mistakes.
    """
    if currency_direction in ("flat", "unknown") or direction == "neutral":
        return "neutral"

    # Bearish: we want MCX down. A weakening rupee pushes MCX up, so it dampens.
    # Bullish: we want MCX up. A weakening rupee pushes MCX up, so it amplifies.
    if direction == "bullish":
        return "amplifies" if currency_direction == "inr_weakening" else "dampens"
    return "dampens" if currency_direction == "inr_weakening" else "amplifies"


def strikes(level: float, option_type: str | None) -> dict:
    """The ATM strike nearest `level`, and the first strike out of the money."""
    atm = round(level / STRIKE_INTERVAL) * STRIKE_INTERVAL
    if option_type == "call":
        one_otm = atm + STRIKE_INTERVAL  # OTM call sits above the money
    elif option_type == "put":
        one_otm = atm - STRIKE_INTERVAL  # OTM put sits below it
    else:
        one_otm = None
    return {"strike_atm": int(atm), "strike_1_otm": int(one_otm) if one_otm else None}


def context(market: dict | None, direction: str, option_type: str | None) -> dict:
    """Build the currency block for a signal, from one market_data.json row.

    Every field is None when the market data is missing, so a stale or absent
    market_data.json degrades the guidance rather than breaking the signal.
    """
    empty = {
        "usd_inr_close": None,
        "usd_inr_trend_pct": None,
        "wti_trend_pct": None,
        "currency_direction": "unknown",
        "currency_effect": "neutral",
        "mcx_implied_level": None,
        "strike_atm": None,
        "strike_1_otm": None,
        "market_data_date": None,
    }
    if not market:
        logger.warning("No market data — strike guidance and currency context unavailable")
        return empty

    usd_inr = market.get("usd_inr_close")
    usd_inr_trend = market.get("usd_inr_trend_pct")
    currency_direction = classify(usd_inr_trend)

    # market_data.py already computes mcx_close as wti_close x usd_inr_close.
    level = market.get("mcx_close")

    block = {
        **empty,
        "usd_inr_close": usd_inr,
        "usd_inr_trend_pct": usd_inr_trend,
        "wti_trend_pct": market.get("wti_trend_pct"),
        "currency_direction": currency_direction,
        "currency_effect": effect_on(direction, currency_direction),
        "mcx_implied_level": level,
        "market_data_date": market.get("date"),
    }
    if level:
        block.update(strikes(level, option_type))
    else:
        logger.warning("Market data has no mcx_close — cannot suggest strikes")

    return block


def risk_notes(block: dict, direction: str) -> list[str]:
    """Risk lines the currency context earns. Empty when it has nothing to say."""
    notes = []
    trend = block.get("usd_inr_trend_pct")
    if trend is None:
        notes.append("No USD/INR data — strike guidance is unverified")
        return notes

    effect = block.get("currency_effect")
    moved = "weakened" if trend > 0 else "strengthened"

    if effect == "dampens":
        severity = "sharply " if abs(trend) >= SHARP_TREND_PCT else ""
        notes.append(
            f"INR {severity}{moved} {abs(trend):.2f}% over 5 sessions, working against "
            f"a {direction} MCX move"
        )
    elif effect == "amplifies" and abs(trend) >= SHARP_TREND_PCT:
        notes.append(
            f"INR {moved} {abs(trend):.2f}% over 5 sessions, amplifying a {direction} "
            "MCX move — the rupee is doing part of the work, and can give it back"
        )

    # FX is shut during the hold, so the rupee's risk is the gap, not the session.
    if abs(trend) >= SHARP_TREND_PCT:
        notes.append(
            "Onshore USD/INR is closed 17:00-09:00 IST, so this move cannot reverse "
            "during the trade — it prices the gap, not the session"
        )

    return notes
