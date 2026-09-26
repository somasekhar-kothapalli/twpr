"""investing.com scraper for the EIA analyst consensus (in.investing.com).

investing.com returns 403 to plain HTTP on every route, including its JSON
endpoints, so this needs a real browser: Playwright drives headless Chromium,
loads the economic calendar once, then calls the calendar's own data service
from inside that session. One request returns every indicator for a date range
with actual / forecast / previous, which beats loading five separate pages —
those get Cloudflare-challenged after the first one.

This is the fallback source. `tradingeconomics_scraper` needs no browser and is
tried first; the two agree exactly on actuals, and within ~0.1 mb on the crude
consensus (different survey panels).

Scope limit: the US calendar carries only `API Weekly Crude Oil Stock` for the
API report — no Cushing, gasoline or distillate legs — so `fetch_api_report`
cannot fill the required four fields and returns None. Trading Economics is the
source for that report.

Requires `playwright` plus a one-off `python -m playwright install chromium`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from common import week_ending

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://in.investing.com/economic-calendar/"
SERVICE_PATH = "/economic-calendar/Service/getCalendarFilteredData"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
US_COUNTRY_ID = "5"
IST_TIMEZONE_ID = "55"

# Exact event names. Matched exactly, never as substrings: "Crude Oil
# Inventories" is a substring of "Cushing Crude Oil Inventories".
EIA_EVENTS = {
    "crude": "Crude Oil Inventories",
    "cushing": "Cushing Crude Oil Inventories",
    "gasoline": "Gasoline Inventories",
    "distillate": "EIA Weekly Distillates Stocks",
}
API_CRUDE_EVENT = "API Weekly Crude Oil Stock"

NAV_TIMEOUT_MS = 90_000


def _parse_mb(text: str) -> float | None:
    """Parse an investing.com figure such as "-0.640M" into million barrels."""
    cleaned = re.sub(r"[\s ]+", "", (text or "").replace("&nbsp;", "")).replace(",", "")
    match = re.match(r"^(-?\d+(?:\.\d+)?)([MBK]?)$", cleaned)
    if not match:
        return None
    value, suffix = float(match.group(1)), match.group(2)
    if suffix == "K":
        return value / 1000.0
    if suffix == "B":
        return value * 1000.0
    return value


def _parse_service_html(html: str) -> list[dict]:
    """Parse the calendar service's HTML fragment into row dicts.

    Kept separate from the browser call so it is testable offline.
    Cells: [time, currency, importance, event, actual, forecast, previous, ...]
    """
    rows = []
    for row_html in re.findall(r'<tr[^>]*id="eventRowId_\d+"[^>]*>.*?</tr>', html, re.S):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).replace("\xa0", " ").strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        ]
        if len(cells) < 7:
            continue
        stamp = re.search(r'data-event-datetime="(\d{4})/(\d{2})/(\d{2})', row_html)
        if not stamp:
            continue
        rows.append(
            {
                "release_date": date(*(int(g) for g in stamp.groups())),
                "event": cells[3],
                "actual": _parse_mb(cells[4]),
                "forecast": _parse_mb(cells[5]),
                "previous": _parse_mb(cells[6]),
            }
        )
    return rows


def _calendar_rows(date_from: str, date_to: str) -> list[dict]:
    """Every US calendar event between the two dates, via headless Chromium.

    Returns dicts of {release_date, event, actual, forecast, previous}.
    """
    from playwright.sync_api import sync_playwright

    script = """
    async ([country, tz, dateFrom, dateTo, path]) => {
      const body = new URLSearchParams({
        'country[]': country, dateFrom, dateTo, timeZone: tz,
        timeFilter: 'timeRemain', currentTab: 'custom', limit_from: '0',
      });
      const response = await fetch(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body,
      });
      if (!response.ok) return JSON.stringify({error: response.status});
      return await response.text();
    }
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                user_agent=USER_AGENT,
                viewport={"width": 1400, "height": 1000},
            )
            page = context.new_page()
            response = page.goto(
                CALENDAR_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
            if response is None or response.status != 200 or "Just a moment" in page.title():
                status = response.status if response else "no response"
                raise RuntimeError(f"investing.com blocked the calendar (status {status})")

            raw = page.evaluate(
                script,
                [US_COUNTRY_ID, IST_TIMEZONE_ID, date_from, date_to, SERVICE_PATH],
            )
        finally:
            browser.close()

    payload = json.loads(raw)
    if "error" in payload:
        raise RuntimeError(f"calendar service returned {payload['error']}")

    rows = _parse_service_html(payload.get("data", ""))
    logger.info("investing.com: %d calendar rows for %s..%s", len(rows), date_from, date_to)
    return rows


def _row_for(rows: list[dict], event: str, target: date) -> dict | None:
    """The row for `event` whose release covers the week ending `target`."""
    for row in rows:
        if row["event"] == event and week_ending(row["release_date"]) == target.isoformat():
            return row
    return None


def fetch_consensus(week: str | None = None) -> dict | None:
    """Analyst consensus for the EIA week ending `week` (default: this week's).

    Returns None when investing.com has not posted a forecast for all three
    legs yet, or when the browser is blocked.
    """
    target = date.fromisoformat(week or week_ending())
    # The EIA report for week ending Friday W lands the following Wednesday, and
    # the API report the Tuesday before it. Span W+3..W+7 to cover both.
    rows = _calendar_rows(
        (target + timedelta(days=3)).isoformat(), (target + timedelta(days=7)).isoformat()
    )

    values: dict[str, float] = {}
    for name in ("crude", "gasoline", "distillate"):
        row = _row_for(rows, EIA_EVENTS[name], target)
        if row is None:
            logger.warning("investing.com: no %s row for week ending %s", name, target)
            return None
        if row["forecast"] is None:
            logger.info("investing.com: %s forecast for %s not posted yet", name, target)
            return None

        values[f"{name}_consensus_mb"] = row["forecast"]
        if name == "crude":
            if row["previous"] is None:
                logger.warning("investing.com: crude previous missing for %s", target)
                return None
            values["crude_previous_mb"] = row["previous"]

    logger.info(
        "investing.com consensus %s: crude %+.3f | gasoline %+.3f | distillate %+.3f",
        target,
        values["crude_consensus_mb"],
        values["gasoline_consensus_mb"],
        values["distillate_consensus_mb"],
    )
    return values


def fetch_api_report(week: str | None = None) -> dict | None:
    """Not available from investing.com — see the module docstring.

    The US calendar carries only the crude leg of the API report, so the four
    fields `api_monitor` requires cannot be filled. Returning a partial dict
    would just be discarded downstream, so this returns None and says why.
    """
    target = date.fromisoformat(week or week_ending())
    logger.info(
        "investing.com carries only the crude leg of the API report — no Cushing, "
        "gasoline or distillate. Skipping %s; use tradingeconomics_scraper.",
        target,
    )
    return None


def fetch_api_crude_only(week: str | None = None) -> float | None:
    """The API crude change alone, for cross-checking another source.

    Not part of the fetcher contract — `api_monitor` never calls this.
    """
    target = date.fromisoformat(week or week_ending())
    rows = _calendar_rows(
        (target + timedelta(days=3)).isoformat(), (target + timedelta(days=7)).isoformat()
    )
    row = _row_for(rows, API_CRUDE_EVENT, target)
    if row is None or row["actual"] is None:
        logger.info("investing.com: API crude for %s not available", target)
        return None
    logger.info("investing.com API crude %s: %+.3f mb", target, row["actual"])
    return row["actual"]
