"""Rupee context for an INR trade on a USD benchmark.

Adapted from the earlier pipeline's currency module (F:/TradeDesk/twpr/app/currency.py). The signal comes
from EIA inventories, which move WTI in USD; the instrument, MCX CrudeOil, is quoted in INR per barrel, so
roughly

    MCX  ~  WTI (USD/bbl)  x  USD/INR

This module names what the rupee has been doing and whether that helps or hurts the trade. It never feeds
the decision (regime, direction, option): onshore USD/INR trades 09:00-17:00 IST only, and the trade is held
roughly 20:00-22:30 IST, so the currency market is shut for the whole hold and the move being traded is the
WTI move. The earlier pipeline measured, over six months of daily data, WTI's mean absolute move at 2.89%
against USD/INR's 0.38% (7.7x), the currency at a median 12% of the combined MCX move, and a sign flip of the
MCX move against WTI on only 3.2% of days. So the rupee matters for the strike level and as a note on the
overnight gap (a position this system never holds), not for the direction of the trade.
"""

# Below this a 5-session move is noise; at or above SHARP it earns a risk line of its own.
FLAT_TREND_PCT = 0.1
SHARP_TREND_PCT = 0.5


def trend_pct(series):
    """Percent change from the oldest to the newest value in `series` (None values are skipped)."""
    clean = [v for v in series if v is not None]
    if len(clean) < 2 or not clean[0]:
        return None
    return round((clean[-1] - clean[0]) / clean[0] * 100, 3)


def classify(usd_inr_trend):
    """The rupee's direction. A weakening rupee means USD/INR rising."""
    if usd_inr_trend is None:
        return "unknown"
    if abs(usd_inr_trend) < FLAT_TREND_PCT:
        return "flat"
    return "inr_weakening" if usd_inr_trend > 0 else "inr_strengthening"


def effect_on(direction, currency_direction):
    """Whether the rupee helps or hurts the trade's MCX move. A weakening rupee lifts the rupee price of
    crude: it works against a bearish trade and with a bullish one. Spelled out per case, because the sign
    conventions invite mistakes."""
    if currency_direction in ("flat", "unknown") or direction == "neutral":
        return "neutral"
    if direction == "bullish":
        return "amplifies" if currency_direction == "inr_weakening" else "dampens"
    return "dampens" if currency_direction == "inr_weakening" else "amplifies"


def risk_notes(usd_inr_trend, direction):
    """The risk lines the rupee earns. Empty when it has nothing to say."""
    if usd_inr_trend is None:
        return ["No USD/INR history: the rupee context is unknown (the WTI-to-MCX conversion still uses the live rate)."]
    notes = []
    effect = effect_on(direction, classify(usd_inr_trend))
    moved = "weakened" if usd_inr_trend > 0 else "strengthened"
    if effect == "dampens":
        severity = "sharply " if abs(usd_inr_trend) >= SHARP_TREND_PCT else ""
        notes.append(f"INR {severity}{moved} {abs(usd_inr_trend):.2f}% over 5 sessions, working against a "
                     f"{direction} MCX move.")
    elif effect == "amplifies" and abs(usd_inr_trend) >= SHARP_TREND_PCT:
        notes.append(f"INR {moved} {abs(usd_inr_trend):.2f}% over 5 sessions, amplifying a {direction} MCX move: the "
                     "rupee is doing part of the work, and can give it back.")
    if abs(usd_inr_trend) >= SHARP_TREND_PCT:
        notes.append("Onshore USD/INR is shut 17:00-09:00 IST, so this cannot reverse during the trade: it prices "
                     "the overnight gap, not the session.")
    return notes


def context(usd_inr_trend, direction):
    """The currency block for a signal.json."""
    currency_direction = classify(usd_inr_trend)
    return {"usd_inr_trend_pct": usd_inr_trend, "direction": currency_direction,
            "effect": effect_on(direction, currency_direction), "notes": risk_notes(usd_inr_trend, direction)}
