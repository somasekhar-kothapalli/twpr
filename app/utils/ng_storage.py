"""Working gas in underground storage (Lower 48) from EIA's public weekly table.

The scrapers only carry the weekly CHANGE. This file also has the total stocks, the implied flow (which differs
from the net change when EIA reclassifies gas between base and working gas), the year-ago stocks and the 5-year
average, so storage can be read against its seasonal norm. No API key. The file holds only the latest report, so
it must be read soon after the release; the signed redirect needs follow_redirects.
"""
import csv
import io
import logging
import re
from datetime import datetime

import httpx

logger = logging.getLogger("twpr.ng_storage")

STORAGE_URL = "https://ir.eia.gov/ngs/wngsr.csv"
_RELEASED = re.compile(r"Released:\s*([A-Za-z]+ \d{1,2}, \d{4})")
_WEEK = re.compile(r"Week Ending\s*([A-Za-z]+ \d{1,2}, \d{4})")
# The Total row's numbers in order: stocks, prior-week stocks, net change, implied flow, year-ago stocks,
# % vs year ago, 5-year-average stocks, % vs 5-year average.
_TOTAL_FIELDS = ("total_bcf", "prior_total_bcf", "net_change_bcf", "implied_flow_bcf", "year_ago_bcf",
                 "pct_vs_year_ago", "five_year_avg_bcf", "pct_vs_five_year_avg")


def _date(text):
    return datetime.strptime(text, "%B %d, %Y").strftime("%d-%m-%Y")


def parse_storage_csv(text):
    """{release_date, week_ending, total_bcf, ..., pct_vs_five_year_avg} (DD-MM-YYYY dates) from the CSV text."""
    text = text.lstrip("﻿")
    released, week = _RELEASED.search(text), _WEEK.search(text)
    if not released or not week:
        raise ValueError("storage CSV: release or week-ending line not found")
    for row in csv.reader(io.StringIO(text)):
        if row and row[0].strip() == "Total":
            numbers = [float(cell.replace(",", "")) for cell in row[1:] if cell.strip()]
            if len(numbers) != len(_TOTAL_FIELDS):
                raise ValueError(f"storage CSV: Total row has {len(numbers)} numbers, expected {len(_TOTAL_FIELDS)}")
            result = {"release_date": _date(released.group(1)), "week_ending": _date(week.group(1)),
                      **dict(zip(_TOTAL_FIELDS, numbers))}
            # Net change is stocks minus last week's; implied flow is what EIA says actually flowed.
            result["reclassified"] = abs(result["net_change_bcf"] - result["implied_flow_bcf"]) > 0.5
            return result
    raise ValueError("storage CSV: no Total row")


def fetch_storage(get=httpx.get):
    """The latest storage report, or None (logged) if the file is unreachable or unparsable."""
    try:
        response = get(STORAGE_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        response.raise_for_status()
        return parse_storage_csv(response.text)
    except Exception as exc:  # noqa: BLE001 - optional input for a record-only script
        logger.warning("EIA storage table unavailable: %s", type(exc).__name__)
        return None
