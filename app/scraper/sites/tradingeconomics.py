"""TradingEconomics indicator-page scraper. Returns the same response as
scraper.sites.investing.InvestingCalendarScraper (shape defined in
scraper.utils.base); only `forecast` may differ. Driven through a real browser
via scraper.browser.

Two table shapes exist on TE indicator pages (verified live against real
markup):

- Calendar table (`tr.an-estimate-row`) - a history + one upcoming row,
  values suffixed "M"/"K", first column is the release date
  (ISO on the page, converted to DD-MM-YYYY in our rows). Only some indicators have this (e.g. API crude does;
  API Cushing/gasoline/distillate don't) - fetch_page() returns None for
  calendar_rows when it's absent, not an error.
- Stats table (`<table class="table">` with an "Actual" header) -
  present on every indicator page, just the latest actual + previous,
  unitless numbers scaled by a separate "Unit" column.
"""
from ..utils.base import CalendarScraper
from ..utils.calendar import format_date, gmt_to_ist, latest_released_row, to_mb_suffixed
from ..utils.cli import run_cli


TE_BASE = "https://tradingeconomics.com/"
# The stats table (has a 'Unit' header) is on every indicator page; some pages (API Cushing
# etc.) have no calendar table, and a hidden empty table.table sits first in the DOM.
STATS_TABLE_SELECTOR = "table.table:has(th:text-is('Unit'))"


class TradingEconomicsScraper(CalendarScraper):
    """TradingEconomics indicator pages.

        with TradingEconomicsScraper() as scraper:
            row = scraper.fetch_release("united-states/crude-oil-stocks-change", "23-09-2026")

    Response shape is defined in scraper.utils.base.
    """

    base_url = TE_BASE
    wait_selector = STATS_TABLE_SELECTOR

    def parse_rows(self, soup):
        return parse_calendar_rows(soup)

    def parse_stats(self, soup, rows):
        # investing.com's "Latest Release" widget is the latest released row, so
        # use the same source. The stats table rounds (2.97 vs 2.969) and has no
        # forecast; it is only the fallback for pages with no calendar table.
        latest = latest_released_row(rows)
        if latest:
            return {"actual_mb": latest["actual"], "forecast_mb": latest["forecast"],
                    "previous_mb": latest["previous"]}
        return parse_stats_table(soup)


def parse_calendar_rows(soup):
    """All `tr.an-estimate-row` rows, in document order, as dicts:
    {release_date, time, actual, forecast, previous} - same shape as
    investing.py's parse_row(), so both sites' calendar rows are
    interchangeable for callers. `time` is the row's GMT column
    converted to IST. `actual`/`forecast`/`previous` are floats in
    million barrels (None for not-yet-released/not-yet-locked cells).
    Empty list if the page has no calendar table at all.
    """
    rows = []
    for row in soup.select("tr.an-estimate-row"):
        tds = row.find_all("td")
        if len(tds) < 8:
            continue
        rows.append({
            "release_date": format_date(tds[0].get_text(strip=True)),
            "time": gmt_to_ist(tds[1].get_text(strip=True)),
            "actual": to_mb_suffixed(tds[4].get_text(strip=True)),
            "forecast": to_mb_suffixed(tds[6].get_text(strip=True)),
            "previous": to_mb_suffixed(tds[5].get_text(strip=True)),
        })
    return rows


def _to_mb_unit_aware(text, unit_text):
    text = text.strip().replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if "thousand" in unit_text.lower():
        value /= 1000.0
    return value


def parse_stats_table(soup):
    """The universal 'Actual | Previous | Highest | Lowest | ... | Unit |
    ...' table every TE indicator page has. Returns {actual_mb,
    previous_mb}, scaled by the page's own Unit column."""
    for table in soup.find_all("table", class_="table"):
        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        # "Unit" disambiguates this from the calendar table (id="calendar"),
        # which also carries class="table" and an "Actual"/"Previous" header
        # pair but no "Unit" column - without this check the calendar table
        # matches first and the real stats table below it is never reached.
        if "Actual" not in headers or "Previous" not in headers or "Unit" not in headers:
            continue
        data_row = table.find("tbody tr") or next(
            (tr for tr in table.find_all("tr") if not tr.find_parent("thead")), None
        )
        if data_row is None:
            continue
        tds = data_row.find_all("td")
        if len(tds) < len(headers):
            continue
        cell = dict(zip(headers, (td.get_text(strip=True) for td in tds)))
        unit_text = cell.get("Unit", "")
        return {
            "actual_mb": _to_mb_unit_aware(cell.get("Actual", ""), unit_text),
            "previous_mb": _to_mb_unit_aware(cell.get("Previous", ""), unit_text),
        }
    return {"actual_mb": None, "previous_mb": None}


if __name__ == "__main__":
    run_cli(TradingEconomicsScraper, "united-states/crude-oil-stocks-change")
