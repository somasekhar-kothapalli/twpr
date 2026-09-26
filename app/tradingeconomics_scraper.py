"""Trading Economics scraper for the EIA consensus and the API private report.

The calendar tables are in the served HTML, so this needs plain HTTP — no
browser. That makes it the reliable source; `investing_scraper` is the fallback.

Each indicator page carries a calendar table shaped:

    [date, time, event, reference, Actual, Previous, Consensus, TEForecast]

Empty cells are meaningful — an empty `Actual` marks an unreleased row — so the
parser keeps cell positions and never filters blanks out.

Both entry points return None rather than a partial dict. A wrong consensus is
worse than no consensus: it silently corrupts every deviation downstream.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime

import httpx

from common import week_ending

logger = logging.getLogger(__name__)

BASE_URL = "https://tradingeconomics.com/united-states/"

# EIA weekly indicators. Cushing publishes no consensus, which is fine: the
# signal engine only needs Cushing's actual, never its forecast.
EIA_SLUGS = {
    "crude": "crude-oil-stocks-change",
    "cushing": "cushing-crude-oil-stocks",
    "gasoline": "gasoline-stocks-change",
    "distillate": "distillate-stocks",
}

# The API crude page carries a calendar table AND a summary table listing all
# four API indicators, so one fetch covers the whole report.
API_SLUG = "api-crude-oil-stock-change"
API_SUMMARY_ROWS = {
    "api_crude_mb": "API Crude Oil Stock Change",
    "api_cushing_mb": "API Cushing Number",
    "api_gasoline_mb": "API Gasoline Stocks",
    "api_distillate_mb": "API Distillate Stocks",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT_SECONDS = 25.0
MAX_RETRIES = 2
BACKOFF_SECONDS = 3.0

THOUSAND_BARRELS = "thousand barrels"


def _fetch(slug: str) -> str:
    """GET one indicator page, retrying transient failures."""
    url = f"{BASE_URL}{slug}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = httpx.get(
                url, headers=HEADERS, timeout=TIMEOUT_SECONDS, follow_redirects=True
            )
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            logger.warning(
                "GET %s failed (attempt %d/%d): %s", url, attempt + 1, MAX_RETRIES + 1, exc
            )
            if attempt == MAX_RETRIES:
                raise
            time.sleep(BACKOFF_SECONDS)
    raise RuntimeError("unreachable")


def _table_rows(html: str) -> list[list[str]]:
    """Every table row as a list of cell texts, blanks preserved."""
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)
        ]
        if cells:
            rows.append(cells)
    return rows


def _parse_mb(text: str, unit: str = "") -> float | None:
    """Parse a Trading Economics figure into million barrels.

    Handles the calendar's "-0.64M" / "2.969M" / "-1M" and the summary table's
    bare "2266.00" paired with a unit column.
    """
    if not text:
        return None

    cleaned = text.replace(",", "").strip()
    match = re.match(r"^(-?\d+(?:\.\d+)?)\s*([MBK]?)$", cleaned)
    if not match:
        return None

    value, suffix = float(match.group(1)), match.group(2)
    if suffix == "K" or THOUSAND_BARRELS in unit.lower():
        return value / 1000.0
    if suffix == "B":
        return value * 1000.0
    return value  # "M", or already million barrels ("BBL/1Million")


def _calendar_rows(html: str) -> list[dict]:
    """Calendar rows as dicts, oldest first.

    Row shape: [date, time, event, reference, actual, previous, consensus, ...]
    """
    parsed = []
    for cells in _table_rows(html):
        if not re.match(r"^20\d\d-\d\d-\d\d$", cells[0]):
            continue
        padded = cells + [""] * (7 - len(cells))
        parsed.append(
            {
                "release_date": padded[0],
                "event": padded[2],
                "reference": padded[3],
                "actual": _parse_mb(padded[4]),
                "previous": _parse_mb(padded[5]),
                "consensus": _parse_mb(padded[6]),
            }
        )
    return parsed


def _reference_to_date(reference: str, release_date: str) -> date | None:
    """Turn a "Sep/11" reference into a date, using the release year.

    A January release referencing December belongs to the previous year.
    """
    match = re.match(r"^([A-Za-z]{3})/(\d{1,2})$", reference.strip())
    if not match:
        return None

    release = datetime.strptime(release_date, "%Y-%m-%d").date()
    try:
        month = datetime.strptime(match.group(1), "%b").month
    except ValueError:
        return None

    year = release.year - 1 if month == 12 and release.month == 1 else release.year
    try:
        return date(year, month, int(match.group(2)))
    except ValueError:
        return None


def _row_for_week(rows: list[dict], target: date) -> dict | None:
    """The calendar row whose reference week matches `target`."""
    for row in rows:
        if _reference_to_date(row["reference"], row["release_date"]) == target:
            return row
    return None


def fetch_consensus(week: str | None = None) -> dict | None:
    """Analyst consensus for the EIA week ending `week` (default: this week's).

    Returns None when Trading Economics has not published a consensus for all
    three legs yet — it typically fills them in close to the release.
    """
    target = date.fromisoformat(week or week_ending())
    values: dict[str, float] = {}

    for name in ("crude", "gasoline", "distillate"):
        rows = _calendar_rows(_fetch(EIA_SLUGS[name]))
        row = _row_for_week(rows, target)
        if row is None:
            references = [r["reference"] for r in rows]
            logger.warning(
                "tradingeconomics: no %s row for week ending %s (saw %s)",
                name, target, references,
            )
            return None

        if row["consensus"] is None:
            logger.info(
                "tradingeconomics: %s consensus for %s not published yet", name, target
            )
            return None

        values[f"{name}_consensus_mb"] = row["consensus"]
        if name == "crude":
            if row["previous"] is None:
                logger.warning("tradingeconomics: crude previous missing for %s", target)
                return None
            values["crude_previous_mb"] = row["previous"]

    logger.info(
        "tradingeconomics consensus %s: crude %+.3f | gasoline %+.3f | distillate %+.3f",
        target,
        values["crude_consensus_mb"],
        values["gasoline_consensus_mb"],
        values["distillate_consensus_mb"],
    )
    return values


def fetch_api_report(week: str | None = None) -> dict | None:
    """The API private inventory report for the week ending `week`.

    The crude calendar row anchors the week; the other three legs come from the
    page's summary table, which holds the same release. Returns None until the
    report is out.
    """
    target = date.fromisoformat(week or week_ending())
    html = _fetch(API_SLUG)

    rows = _calendar_rows(html)
    crude_row = _row_for_week(rows, target)
    if crude_row is None:
        logger.warning("tradingeconomics: no API crude row for week ending %s", target)
        return None
    if crude_row["actual"] is None:
        logger.info("tradingeconomics: API report for %s not released yet", target)
        return None

    # The summary table is undated: its "Last" column is always the most recent
    # release. So its three non-crude legs only belong to `target` when target
    # IS the latest released week. Otherwise they are a later week's numbers and
    # would be silently wrong.
    released = [r for r in rows if r["actual"] is not None]
    latest = max(
        (d for r in released if (d := _reference_to_date(r["reference"], r["release_date"]))),
        default=None,
    )
    if latest != target:
        logger.warning(
            "tradingeconomics: %s is not the latest released API week (%s) — the summary "
            "table would give that week's Cushing/gasoline/distillate, so discarding",
            target, latest,
        )
        return None

    # Summary table shape: [indicator, Last, Previous, Unit, Reference]
    summary: dict[str, float] = {}
    for cells in _table_rows(html):
        if len(cells) < 4:
            continue
        for field, label in API_SUMMARY_ROWS.items():
            if cells[0].strip() == label:
                parsed = _parse_mb(cells[1], cells[3])
                if parsed is not None:
                    summary[field] = parsed

    # Trust the dated calendar row for crude over the undated summary row.
    summary["api_crude_mb"] = crude_row["actual"]

    missing = [f for f in API_SUMMARY_ROWS if f not in summary]
    if missing:
        logger.warning("tradingeconomics: API summary missing %s — discarding", missing)
        return None

    logger.info(
        "tradingeconomics API report %s: crude %+.3f | Cushing %+.3f | "
        "gasoline %+.3f | distillate %+.3f",
        target,
        summary["api_crude_mb"],
        summary["api_cushing_mb"],
        summary["api_gasoline_mb"],
        summary["api_distillate_mb"],
    )
    return summary
