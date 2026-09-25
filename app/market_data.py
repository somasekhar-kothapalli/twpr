"""Fetch daily market data from yfinance and the EIA API, then publish it.

Runs 09:00 IST on weekdays. Pulls the last 5 trading days so a missed day is
backfilled on the next run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta

import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from common import DATA_DIR, now_utc, setup_logging, write_json
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

EIA_FUTURES_URL = "https://api.eia.gov/v2/petroleum/pri/fut/data/"
EIA_SERIES = {"RCLC1": "wti_m1_price", "RCLC2": "wti_m2_price"}

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


def fetch_eia_futures(api_key: str, start: str) -> pd.DataFrame:
    """WTI M1/M2 continuous futures prices from the EIA API, indexed by date string."""
    params = {
        "api_key": api_key,
        "frequency": "daily",
        "data[0]": "value",
        "start": start,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": "100",
    }
    # httpx repeats a list value as facets[series][]=RCLC1&facets[series][]=RCLC2
    params_list = list(params.items()) + [("facets[series][]", s) for s in EIA_SERIES]

    response = httpx.get(EIA_FUTURES_URL, params=params_list, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    rows = response.json()["response"]["data"]
    if not rows:
        logger.warning("EIA futures returned no rows from %s", start)
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    frame = frame[frame["series"].isin(EIA_SERIES)]
    pivoted = frame.pivot_table(index="period", columns="series", values="value", aggfunc="last")
    pivoted = pivoted.rename(columns=EIA_SERIES).astype(float)
    logger.info("EIA futures: %d dates, latest %s", len(pivoted), pivoted.index.max())
    return pivoted


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
    """Merge yfinance and EIA data on date and derive the calculated columns."""
    closes = fetch_yfinance(lookback_days)

    api_key = os.getenv("EIA_API_KEY")
    if api_key:
        start = (now_utc().date() - timedelta(days=lookback_days * 3)).isoformat()
        try:
            eia = fetch_eia_futures(api_key, start)
            closes = closes.join(eia, how="left")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.error("EIA futures fetch failed, continuing without M1/M2: %s", exc)
    else:
        logger.warning("EIA_API_KEY not set — wti_m1_price/wti_m2_price will be null")

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
