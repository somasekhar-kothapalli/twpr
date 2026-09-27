"""Fetch daily market data from yfinance and publish it.

Runs 09:00 IST on weekdays. Pulls the last 5 trading days so a missed day is
backfilled on the next run.

The WTI M1-M2 spread comes from the two nearest dated NYMEX contracts rather than
the EIA API: EIA's RCLC1/RCLC2 futures series exist but stopped publishing on
2024-04-05, so any start date after that returns zero rows. Dated contracts also
give the real curve instead of a continuous splice.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from common import DATA_DIR, now_utc, setup_logging, write_json
from currency import classify, trend_pct
from expiry import refresh_cache
from petrocore_client import PetroCoreClient
from telegram_bot import send_error

load_dotenv()

logger = logging.getLogger(__name__)

MARKET_DATA_FILE = DATA_DIR / "market_data.json"

TICKERS = {
    "CL=F": "wti_close",  # WTI front month
    "BZ=F": "brent_close",  # Brent crude
    "DX-Y.NYB": "dxy_close",  # US Dollar Index
    "INR=X": "usd_inr_close",  # USD/INR spot
    "RB=F": "rbob_close",  # RBOB gasoline, $/gallon
    "HO=F": "heating_oil_close",  # Heating oil, $/gallon
}

# NYMEX delivery-month codes, Jan..Dec. CL contracts are monthly.
MONTH_CODES = "FGHJKMNQUVXZ"
CONTRACT_TEMPLATE = "CL{code}{year:02d}.NYM"
# How many months ahead to offer as candidates. Expired contracts 404 and drop
# out, so this only has to be wide enough to cover the two live front months.
CONTRACT_CANDIDATES = 8

GALLONS_PER_BARREL = 42
LOOKBACK_DAYS = 5
REQUEST_TIMEOUT_SECONDS = 30.0


def fetch_yfinance(lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Daily closes for every ticker, indexed by date string. Newest last."""
    # Ask for extra calendar days so weekends/holidays still yield 5 sessions.
    raw = yf.download(
        list(TICKERS),
        period=f"{lookback_days * 3}d",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if raw.empty:
        raise RuntimeError("yfinance returned no rows")

    closes = raw["Close"].rename(columns=TICKERS)
    # Forward-fill: FX trades on days the futures pits are shut, and vice versa.
    closes = closes.ffill().dropna(how="all").tail(lookback_days)
    closes.index = [d.date().isoformat() for d in closes.index]
    return closes


def contract_tickers(as_of: date, count: int = CONTRACT_CANDIDATES) -> list[str]:
    """Candidate NYMEX WTI contract tickers from `as_of`'s month forward."""
    tickers = []
    year, month = as_of.year, as_of.month
    for _ in range(count):
        tickers.append(
            CONTRACT_TEMPLATE.format(code=MONTH_CODES[month - 1], year=year % 100)
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return tickers


def fetch_front_contracts(lookback_days: int = LOOKBACK_DAYS, as_of: date | None = None) -> pd.DataFrame:
    """Closes for the two nearest live WTI contracts, as wti_m1_price / wti_m2_price.

    Contracts that have already expired return nothing from yfinance, so the live
    ones identify themselves: order the candidates by delivery month and take the
    first two that actually have data.
    """
    candidates = contract_tickers(as_of or now_utc().date())

    # Expired contracts 404, which is how the live ones identify themselves — so
    # yfinance's ERROR lines for them are the mechanism working, not a fault.
    # Quieten it for this call only; real yfinance problems elsewhere still show.
    yf_logger = logging.getLogger("yfinance")
    previous_level = yf_logger.level
    yf_logger.setLevel(logging.CRITICAL)
    try:
        raw = yf.download(
            candidates,
            period=f"{lookback_days * 3}d",
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
    finally:
        yf_logger.setLevel(previous_level)
    if raw.empty:
        logger.warning("No WTI contract data returned — M1/M2 will be null")
        return pd.DataFrame()

    closes = raw["Close"] if "Close" in raw else pd.DataFrame()
    # Keep candidate order (nearest delivery first), drop contracts with no data.
    live = [t for t in candidates if t in closes.columns and closes[t].notna().any()]
    if len(live) < 2:
        logger.warning("Fewer than two live WTI contracts (%s) — M1/M2 will be null", live)
        return pd.DataFrame()

    m1, m2 = live[0], live[1]
    logger.info("WTI front contracts: M1 %s, M2 %s", m1, m2)

    frame = closes[[m1, m2]].rename(columns={m1: "wti_m1_price", m2: "wti_m2_price"})
    frame = frame.dropna(how="all")
    frame.index = [d.date().isoformat() for d in frame.index]
    return frame


def _check_front_month(closes: pd.DataFrame) -> None:
    """Warn when M1 and the continuous front month disagree.

    `CL=F` is the front-month continuous, so it should equal the nearest dated
    contract. A gap means the contract roll was misread and the spread would be
    measured off the wrong pair.
    """
    if "wti_close" not in closes or "wti_m1_price" not in closes:
        return
    latest = closes.dropna(subset=["wti_close", "wti_m1_price"]).tail(1)
    if latest.empty:
        return

    spot = float(latest["wti_close"].iloc[0])
    m1 = float(latest["wti_m1_price"].iloc[0])
    if spot and abs(m1 - spot) / spot > 0.01:
        logger.warning(
            "M1 %.2f is more than 1%% from the continuous front month %.2f — "
            "check the contract roll before trusting wti_m1m2_spread",
            m1, spot,
        )


def calculate_derived(row: dict) -> dict:
    """Add spreads, the 3-2-1 crack, and the approximate MCX close to one day's row."""
    wti = row.get("wti_close")
    m1, m2 = row.get("wti_m1_price"), row.get("wti_m2_price")

    # Positive = backwardation (tight market); negative = contango (oversupplied).
    row["wti_m1m2_spread"] = round(m1 - m2, 3) if m1 is not None and m2 is not None else None

    rbob, heating_oil = row.get("rbob_close"), row.get("heating_oil_close")
    if None not in (rbob, heating_oil, wti):
        row["crack_321"] = round(
            (
                2 * rbob * GALLONS_PER_BARREL
                + 1 * heating_oil * GALLONS_PER_BARREL
                - 3 * wti
            )
            / 3,
            3,
        )
    else:
        row["crack_321"] = None

    brent = row.get("brent_close")
    row["brent_wti_spread"] = round(brent - wti, 3) if None not in (brent, wti) else None

    usd_inr = row.get("usd_inr_close")
    # MCX CrudeOil is quoted in INR per barrel; this is the textbook approximation,
    # not the exchange's own settlement. Replace once Angel One SmartAPI is wired up.
    row["mcx_close"] = round(wti * usd_inr, 2) if None not in (wti, usd_inr) else None
    row["mcx_source"] = "calculated"

    row["source"] = "yfinance+eia_api"
    row["fetched_at"] = now_utc().isoformat()
    return row


def build_rows(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Merge spot closes and the front two contracts, then derive the columns."""
    closes = fetch_yfinance(lookback_days)

    try:
        contracts = fetch_front_contracts(lookback_days)
        if not contracts.empty:
            closes = closes.join(contracts, how="left")
            # The spot columns are already forward-filled, so the newest row can be
            # a weekend or holiday date the contracts have no print for. Fill them
            # the same way: with the pits shut, the last known curve is the curve.
            closes[["wti_m1_price", "wti_m2_price"]] = closes[
                ["wti_m1_price", "wti_m2_price"]
            ].ffill()
            _check_front_month(closes)
    except Exception as exc:  # noqa: BLE001 — the curve is a nice-to-have, not the row
        logger.error("Front-contract fetch failed, continuing without M1/M2: %s", exc)

    rows = []
    for date_str, series in closes.iterrows():
        row = {"date": date_str}
        row.update({k: (None if pd.isna(v) else round(float(v), 4)) for k, v in series.items()})
        rows.append(calculate_derived(row))
    return rows


def main() -> int:
    """Fetch, save and publish the latest market data rows."""
    setup_logging()
    parser = argparse.ArgumentParser(description="TWPR daily market data fetcher")
    parser.add_argument(
        "--days", type=int, default=LOOKBACK_DAYS, help="trading days to fetch (default 5)"
    )
    args = parser.parse_args()

    try:
        rows = build_rows(args.days)
        if not rows:
            raise RuntimeError("no market data rows produced")

        latest = rows[-1]
        # Only the latest row is saved, so carry the trends across the window with
        # it — signal_engine reads this file for currency context and would
        # otherwise have a single day and no sense of direction.
        latest["usd_inr_trend_pct"] = trend_pct([r.get("usd_inr_close") for r in rows])
        latest["wti_trend_pct"] = trend_pct([r.get("wti_close") for r in rows])
        latest["trend_sessions"] = len(rows)

        write_json(MARKET_DATA_FILE, latest)
        logger.info(
            "Market data %s: WTI %s | Brent %s | USDINR %s | MCX~%s | crack321 %s",
            latest["date"],
            latest.get("wti_close"),
            latest.get("brent_close"),
            latest.get("usd_inr_close"),
            latest.get("mcx_close"),
            latest.get("crack_321"),
        )
        logger.info(
            "Trends over %d sessions: WTI %+.2f%% | USDINR %+.2f%% (%s)",
            latest["trend_sessions"],
            latest["wti_trend_pct"] or 0.0,
            latest["usd_inr_trend_pct"] or 0.0,
            classify(latest["usd_inr_trend_pct"]),
        )

        # Keep the option expiry calendar warm here, on the 09:00 run, so the
        # Wednesday signal path reads a file instead of the network.
        refresh_cache()

        client = PetroCoreClient()
        for row in rows:
            client.post_market_data(row)
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard
        logger.exception("market_data.py failed")
        send_error("market_data.py", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
