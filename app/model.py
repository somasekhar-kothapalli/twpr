"""The runbook's pure model maths (docs/WPSR_WEDNESDAY_RUNBOOK.md section 3). No I/O, no clock.

    TLS   = dS_crude + w_g * dS_mogas + w_d * dS_dist     (dS = actual - consensus, million barrels)
    Z_TLS = TLS / sigma_forecast                            (sigma: rolling std dev of past TLS)
"""
import statistics
from datetime import datetime

from app.utils.common import DATE_FORMAT

SIGMA_WEEKS = 12      # rolling window (runbook: 12 weeks)
SIGMA_MIN_WEEKS = 8   # fewer than this is not a std dev worth gating a trade on


def omega_gasoline(month):
    """0.67, or 0.80 May-Sep (summer driving)."""
    return 0.80 if 5 <= month <= 9 else 0.67


def omega_distillate(month):
    """0.50, or 0.70 Nov-Feb (winter heating)."""
    return 0.70 if month in (11, 12, 1, 2) else 0.50


def tls(crude_surprise, gasoline_surprise, distillate_surprise, month):
    """Total Liquid Surprise in million barrels."""
    return (crude_surprise + omega_gasoline(month) * gasoline_surprise
            + omega_distillate(month) * distillate_surprise)


def history_tls(row):
    """TLS of one history row {release_date, crude_surprise_mb, gasoline_surprise_mb,
    distillate_surprise_mb}, weighted for the month that week was released."""
    month = datetime.strptime(row["release_date"], DATE_FORMAT).month
    return tls(row["crude_surprise_mb"], row["gasoline_surprise_mb"], row["distillate_surprise_mb"], month)


SIGMA_METHODS = ("mad", "std")
MAD_TO_SIGMA = 1.4826   # scales a median absolute deviation to a std dev for normal data


def sigma_forecast(history, weeks=SIGMA_WEEKS, min_weeks=SIGMA_MIN_WEEKS, method="mad"):
    """Spread of the last `weeks` weekly TLS values in `history`, the yardstick for the Z gate.

    method "mad" (default): 1.4826 x median absolute deviation. One freak week (the 12-08-2026
        TLS was +20 mb) cannot inflate the noise floor for the next 12 weeks and lock out every
        real signal; it is the runbook's std dev with outliers discounted.
    method "std": the plain sample standard deviation (the runbook's literal wording). Also the
        fallback when the MAD is 0 (identical values), which would make every Z infinite.

    ValueError if fewer than `min_weeks` weeks exist or the method is unknown: never gate a
    trade on a guess."""
    if method not in SIGMA_METHODS:
        raise ValueError(f"sigma method must be one of {SIGMA_METHODS}, got {method!r}")
    recent = sorted(history, key=lambda r: datetime.strptime(r["release_date"], DATE_FORMAT))[-weeks:]
    if len(recent) < min_weeks:
        raise ValueError(f"sigma_forecast needs {min_weeks} weeks of surprise history, have {len(recent)} "
                         "- run: python -m app.surprise_history --backfill")
    values = [history_tls(r) for r in recent]
    if method == "mad":
        median = statistics.median(values)
        mad = MAD_TO_SIGMA * statistics.median(abs(v - median) for v in values)
        if mad > 0:
            return mad
    return statistics.stdev(values)


def z_score(tls_value, sigma):
    return tls_value / sigma


# ------------------------------------------------------------------ runbook section 3

Z_MIN = 1.25                     # trade only if |Z_TLS| >= this (inclusive)
API_PREPOSITIONED_MB = 3.0       # |API - consensus| above this: the market is pre-positioned
SANITY_PER_MB_USD = (0.15, 0.30)  # empirical anchor: USD/bbl move per 1.0 mb of TLS


def cushing_multiplier(level_mb):
    """Cushing tiered rule (level only; the contradiction check comes first, in `cushing_contradicts`).
    <22 mb: 1.5 -> 2.0x (full at <=10); 22-30: linear 1.5 -> 1.0; 30-45 and >45: 1.0.
    An unknown level is 1.0 - the caller records that it was unknown."""
    if level_mb is None:
        return 1.0
    if level_mb < 22:
        return min(2.0, max(1.5, 1.5 + 0.5 * (22 - level_mb) / 12))
    if level_mb < 30:
        return 1.0 + 0.5 * (30 - level_mb) / 8
    return 1.0


def cushing_contradicts(tls_value, cushing_change_mb):
    """True if Cushing moves against the headline: a build (TLS > 0) with a Cushing draw, or a
    draw with a Cushing build. Exactly 0 contradicts nothing; None when Cushing is unknown."""
    if cushing_change_mb is None:
        return None
    return (tls_value > 0 and cushing_change_mb < 0) or (tls_value < 0 and cushing_change_mb > 0)


def beta_vol(atr_20, ovx):
    """Price scalar: (ATR_20 / 10) * sqrt(OVX / 30)."""
    return (atr_20 / 10) * (ovx / 30) ** 0.5


def expected_move_usd(tls_value, beta, multiplier):
    """Expected WTI move in USD/bbl: -TLS * beta_vol * Cushing multiplier (a build is negative)."""
    return -tls_value * beta * multiplier


def sanity_ok(tls_value, move_usd):
    """Step 6: is the move within the 0.15-0.30 USD per mb of TLS anchor?"""
    per_mb = abs(move_usd) / abs(tls_value)
    return SANITY_PER_MB_USD[0] <= per_mb <= SANITY_PER_MB_USD[1]


def classify(tls_value, z, cushing_is_contradicting, crude_change, crude_consensus, api_crude):
    """(regime, direction), regime None = stand down. Direction is the trade's:
      stand down  |Z| < 1.25                                   neutral
      Regime 2    Cushing contradicts the headline (checked first): the FADE, opposite of headline
      Regime 3    EIA draw beat consensus but fell short of an extreme (>3.0 mb) API draw: sell the fact
      Regime 1    otherwise: with the headline (build = bearish, draw = bullish)
    Regime 3's overnight-rally condition (> $1.00) cannot be read from data: it goes on the checklist."""
    if abs(z) < Z_MIN:
        return None, "neutral"
    headline = "bearish" if tls_value > 0 else "bullish"
    if cushing_is_contradicting:
        return 2, "bullish" if headline == "bearish" else "bearish"
    api_surprise = api_crude - crude_consensus
    if (tls_value < 0 and crude_change < crude_consensus and api_surprise < -API_PREPOSITIONED_MB
            and crude_change > api_crude):
        return 3, "bearish"
    return 1, headline
