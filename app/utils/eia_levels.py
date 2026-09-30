"""Cushing crude stock LEVEL (million barrels) from EIA's public weekly table.

The scrapers only carry the weekly CHANGE, but the runbook's Cushing multiplier needs
the level. EIA publishes it on a plain HTML page (no API key), refreshed with the WPSR.
The last column is checked against the change we already have (level[-1] - level[-2]
must equal it), so a stale page - last week's level - is never returned as this week's.
"""
import logging
import re
import time

import httpx

logger = logging.getLogger("twpr.eia_levels")

CUSHING_URL = "https://www.eia.gov/dnav/pet/pet_stoc_wstk_dcu_YCUOK_w.htm"
ROW_LABEL = "Commercial Crude Oil (Excl. Lease Stock)"
TOLERANCE_MB = 0.002   # the page is in thousand barrels; the change we compare to is rounded
_DATE = re.compile(r"\b\d\d/\d\d/\d\d\b")
_NUMBER = re.compile(r"\d[\d,]*")


def parse_cushing_levels(html):
    """[(week_ending 'MM/DD/YY', level_mb), ...] oldest first, from the page's table."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;", " ", html))
    dates = _DATE.findall(text)
    _, _, after = text.partition(ROW_LABEL)
    values = []
    for token in after.split():
        if not _NUMBER.fullmatch(token):
            break
        values.append(int(token.replace(",", "")) / 1000)
    if not dates or len(values) != len(dates):
        raise ValueError(f"unexpected Cushing table: {len(dates)} dates, {len(values)} values")
    return list(zip(dates, values))


def cushing_level(expected_change_mb, get=httpx.get, tries=3, wait_s=20, sleep=time.sleep):
    """The Cushing level for the week whose change is `expected_change_mb`, or None (logged)
    if the page is unreachable, unparsable, or still shows the previous week."""
    for attempt in range(1, tries + 1):
        try:
            response = get(CUSHING_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            levels = parse_cushing_levels(response.text)
            (_, previous), (week, level) = levels[-2], levels[-1]
            if abs((level - previous) - expected_change_mb) <= TOLERANCE_MB:
                logger.info("Cushing level %.3f mb (week ending %s)", level, week)
                return round(level, 3)
            problem = (f"page shows a {level - previous:+.3f} mb change for week ending {week}, "
                       f"expected {expected_change_mb:+.3f} (not updated yet?)")
        except Exception as exc:  # noqa: BLE001 - optional input; never sinks the report
            problem = f"{type(exc).__name__}: {exc}"
        logger.warning("Cushing level attempt %d/%d: %s", attempt, tries, problem)
        if attempt < tries:
            sleep(wait_s)
    return None
