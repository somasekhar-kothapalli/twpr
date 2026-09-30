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


def sigma_forecast(history, weeks=SIGMA_WEEKS, min_weeks=SIGMA_MIN_WEEKS):
    """Sample std dev of the last `weeks` weekly TLS values in `history`.
    ValueError if fewer than `min_weeks` weeks exist: never gate a trade on a guess."""
    recent = sorted(history, key=lambda r: datetime.strptime(r["release_date"], DATE_FORMAT))[-weeks:]
    if len(recent) < min_weeks:
        raise ValueError(f"sigma_forecast needs {min_weeks} weeks of surprise history, have {len(recent)} "
                         "- run: python -m app.surprise_history --backfill")
    return statistics.stdev(history_tls(r) for r in recent)


def z_score(tls_value, sigma):
    return tls_value / sigma
