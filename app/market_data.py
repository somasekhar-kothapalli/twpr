"""Market inputs for the WPSR model -> data/market.json (yfinance, no scraping).

    atr_20        20-day average true range of WTI front month, USD/bbl  (beta_vol input)
    ovx           Cboe crude oil volatility index                        (beta_vol + strike delta)
    cl1_cl2       front minus second WTI contract, USD/bbl               (scorecard, time-spread filter)
    crack_321     3:2:1 crack spread, USD/bbl                            (scorecard)
    overnight_rally_usd  WTI move from the API print (Tue 16:30 ET) to the EIA print   (Regime 3)
    usd_inr_trend_pct    USD/INR change over the last 5 sessions                        (rupee context)
    brent_wti     Brent minus WTI, USD/bbl                               (scorecard)
    dxy           US dollar index                                        (context)

atr_20 and ovx are required: without them the model cannot size the expected move,
so the run fails loudly (Telegram alert, exit 1, nothing written). The scorecard
values are context; any that cannot be read become null.

Run from the repo root:
    python -m app.market_data                      # now (run shortly before the print)
    python -m app.market_data --date 23-09-2026    # replay: as of 10:29 ET on that release day
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app import currency
from app.utils.common import DATE_FORMAT, MARKET_FILE, ROOT, fmt, fmt_ts, now_ist, now_utc, setup_logging, write_json
from app.utils.telegram import send_exception

logger = logging.getLogger("twpr.market_data")

ATR_DAYS = 20
FETCH_TIMEOUT_S = 15
MONTH_CODES = "FGHJKMNQUVXZ"   # CME futures month codes, Jan..Dec
FRONT_TOLERANCE = 0.25         # USD; a live quote can move between two fetches
BARRELS_PER_GALLON = 42       # RBOB and heating oil are quoted in USD/gallon
NEW_YORK = ZoneInfo("America/New_York")
API_TIME_ET = (16, 30)         # API report, Tuesday
PRINT_ET = (10, 30)            # EIA report, the next morning
MAX_BAR_GAP = timedelta(minutes=45)   # older than this and there is no price "at" the moment


def true_ranges(bars):
    """True range per bar after the first: max(high-low, |high-prev close|, |low-prev close|).
    `bars` is a list of (high, low, close), oldest first."""
    return [max(h - l, abs(h - pc), abs(l - pc)) for (h, l, _), (_, _, pc) in zip(bars[1:], bars)]


def atr(bars, days=ATR_DAYS):
    """Simple average of the last `days` true ranges. ValueError if there are too few bars.
    (Simple mean, not Wilder smoothing: the runbook only says "20-day ATR".)"""
    ranges = true_ranges(bars)
    if len(ranges) < days:
        raise ValueError(f"need {days + 1} daily bars for a {days}-day ATR, got {len(bars)}")
    return sum(ranges[-days:]) / days


def crack_321(wti, rbob, heating_oil):
    """3:2:1 crack: (2 gasoline + 1 distillate - 3 crude) / 3, products converted to USD/bbl."""
    return (2 * rbob * BARRELS_PER_GALLON + heating_oil * BARRELS_PER_GALLON - 3 * wti) / 3


def contract_symbols(today, count=4):
    """The next `count` WTI contract tickers after `today`'s month, e.g. CLX26.NYM."""
    index = today.year * 12 + today.month - 1     # months since year 0, zero-based
    symbols = []
    for step in range(1, count + 1):
        year, month = divmod(index + step, 12)
        symbols.append(f"CL{MONTH_CODES[month]}{year % 100:02d}.NYM")
    return symbols


def front_second(front_close, closes_by_symbol):
    """(front, second) contract closes. `closes_by_symbol` maps the ordered contract tickers to
    their last close; the front is the one trading at CL=F's price (within FRONT_TOLERANCE,
    since the quotes are fetched a moment apart), the second is the ticker after it. Both
    come from the contract quotes so the spread is internally consistent. None if the
    front can't be identified (never guess the spread)."""
    symbols = list(closes_by_symbol)
    for i, symbol in enumerate(symbols[:-1]):
        close = closes_by_symbol[symbol]
        if close is not None and abs(close - front_close) <= FRONT_TOLERANCE:
            second = closes_by_symbol[symbols[i + 1]]
            return (close, second) if second is not None else None
    return None


def last_api_time(now):
    """The most recent Tuesday 16:30 New York time at or before `now` (an aware datetime)."""
    local = now.astimezone(NEW_YORK)
    tuesday = (local - timedelta(days=(local.weekday() - 1) % 7)).replace(
        hour=API_TIME_ET[0], minute=API_TIME_ET[1], second=0, microsecond=0)
    return tuesday if tuesday <= local else tuesday - timedelta(days=7)


def price_at(bars, moment):
    """Open of the last bar starting at or before `moment`, or None if that bar is more than
    MAX_BAR_GAP old (a gap in the data is not a price). `bars` = [(aware start time, open)]."""
    earlier = [(start, price) for start, price in bars if start <= moment]
    if not earlier or moment - earlier[-1][0] > MAX_BAR_GAP:
        return None
    return earlier[-1][1]


def overnight_rally(bars, now):
    """WTI's move from the API print (Tuesday 16:30 ET) to just before the EIA print (10:30 ET the
    next morning, or `now` if that is earlier), or None if either price is missing. Assumes the
    usual Tuesday -> Wednesday pair: a holiday-shifted release is not modelled."""
    api_time = last_api_time(now)
    print_time = min(now.astimezone(NEW_YORK), (api_time + timedelta(days=1)).replace(
        hour=PRINT_ET[0], minute=PRINT_ET[1]))
    # the bar that opens AT the print already contains the reaction, so stop one second earlier
    start, end = price_at(bars, api_time), price_at(bars, print_time - timedelta(seconds=1))
    if start is None or end is None:
        return None
    return {"rally_usd": round(end - start, 2), "api_price": round(start, 2), "pre_print_price": round(end, 2)}


def replay_moment(release_date):
    """DD-MM-YYYY -> 10:29 New York time that day: one minute before the print, the moment a
    live run should have happened."""
    day = datetime.strptime(release_date, DATE_FORMAT)
    return datetime(day.year, day.month, day.day, PRINT_ET[0], PRINT_ET[1] - 1, tzinfo=NEW_YORK)


def intraday_bars(ticker, asof=None, interval="5m"):
    """[(aware start time, open)] for a yfinance ticker, oldest first. With `asof` (a replay), the
    days around it (Yahoo keeps 5-minute bars for about 60 days)."""
    import yfinance as yf
    if asof is None:
        frame = yf.Ticker(ticker).history(period="5d", interval=interval, timeout=FETCH_TIMEOUT_S).dropna()
    else:
        day = asof.date()
        frame = yf.Ticker(ticker).history(start=day - timedelta(days=5), end=day + timedelta(days=1),
                                          interval=interval, timeout=FETCH_TIMEOUT_S).dropna()
    if frame.empty:
        raise RuntimeError(f"no intraday data for {ticker}")
    return [(ts.to_pydatetime(), float(row.Open)) for ts, row in zip(frame.index, frame.itertuples())]


def history(ticker, period="3mo", before=None, lookback_days=150):
    """(bars, dates) for a yfinance ticker: (high, low, close) per day and the matching dates, oldest
    first. With `before` (a date, for a replay) only days strictly before it."""
    import yfinance as yf
    if before is None:
        frame = yf.Ticker(ticker).history(period=period, timeout=FETCH_TIMEOUT_S).dropna()
    else:
        frame = yf.Ticker(ticker).history(start=before - timedelta(days=lookback_days), end=before,
                                          timeout=FETCH_TIMEOUT_S).dropna()
    if frame.empty:
        raise RuntimeError(f"no data for {ticker}")
    return [(float(r.High), float(r.Low), float(r.Close)) for r in frame.itertuples()], [d.date() for d in frame.index]


def last_close(ticker, before=None):
    bars, dates = history(ticker, "5d", before, lookback_days=10)
    return bars[-1][2], dates[-1]


def optional(name, fn, quiet=False):
    """fn() -> value, or None (logged) if it fails: a scorecard input never sinks the run.
    `quiet` logs at debug level: for probes where a miss is normal (an expired contract)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - any failure means "not available"
        (logger.debug if quiet else logger.warning)("%s unavailable: %s", name, exc)
        return None


def fetch_market(today=None, asof=None):
    """The market inputs now, or - with `asof` (an aware datetime, see replay_moment) - as they stood
    then: only days before that date, and the overnight rally up to that moment."""
    if asof is not None:
        today, before, now = asof.date(), asof.date(), asof
    else:
        today, before, now = today or now_ist().date(), None, now_utc()
    bars, dates = history("CL=F", before=before)
    wti, as_of = bars[-1][2], dates[-1]      # latest quote (live if the session is open)
    if dates[-1] >= today:                   # today's bar is still forming: ATR uses completed days only
        bars = bars[:-1]
    market = {
        "as_of": fmt(as_of),
        "wti": round(wti, 2),
        "atr_20": round(atr(bars), 3),
        "ovx": round(last_close("^OVX", before)[0], 2),
    }

    def spread():
        closes = {s: optional(s, lambda s=s: last_close(s, before)[0], quiet=True) for s in contract_symbols(today)}
        pair = front_second(wti, closes)
        if pair is None:
            raise RuntimeError("could not identify the front contract")
        return round(pair[0] - pair[1], 2)

    market["cl1_cl2"] = optional("CL1-CL2", spread)
    market["brent_wti"] = optional("Brent-WTI", lambda: round(last_close("BZ=F", before)[0] - wti, 2))
    market["crack_321"] = optional("3:2:1 crack", lambda: round(crack_321(wti, last_close("RB=F", before)[0], last_close("HO=F", before)[0]), 2))
    market["dxy"] = optional("DXY", lambda: round(last_close("DX-Y.NYB", before)[0], 2))

    def inr_trend():   # USD/INR over the last 5 sessions (6 daily closes)
        bars, _ = history("INR=X", before=before, lookback_days=20)
        return currency.trend_pct([b[2] for b in bars][-6:])
    market["usd_inr_trend_pct"] = optional("USD/INR trend", inr_trend)
    move = optional("overnight rally", lambda: overnight_rally(intraday_bars("CL=F", asof), now))
    market["overnight_rally_usd"] = move["rally_usd"] if move else None
    market["overnight_api_price"] = move["api_price"] if move else None
    market["overnight_pre_print_price"] = move["pre_print_price"] if move else None
    market["fetched_at"] = fmt_ts(asof.astimezone(now_ist().tzinfo) if asof else now_ist())
    return market


def main(argv=None):
    setup_logging()
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Market inputs for the WPSR model")
    parser.add_argument("--date", type=replay_moment, help="replay a release, DD-MM-YYYY: the inputs as of 10:29 ET that day")
    args = parser.parse_args(argv)
    try:
        market = fetch_market(asof=args.date)
        write_json(MARKET_FILE, market)
    except Exception as exc:  # noqa: BLE001 - alert instead of failing silently
        logger.error("market data failed: %s", exc)
        send_exception("market_data.py", exc)
        return 1
    logger.info("market %s: WTI %.2f | ATR20 %.3f | OVX %.2f | CL1-CL2 %s | crack %s | Brent-WTI %s | DXY %s | overnight rally %s",
                market["as_of"], market["wti"], market["atr_20"], market["ovx"], market["cl1_cl2"],
                market["crack_321"], market["brent_wti"], market["dxy"], market.get("overnight_rally_usd"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
