"""Parser tests for both consensus sources.

Offline by design — the HTML below is trimmed from real responses captured on
2026-09-26. CI must not depend on either site being reachable.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import investing_scraper as inv  # noqa: E402
import tradingeconomics_scraper as te  # noqa: E402

# Trading Economics calendar table.
# Columns: date | time | event | reference | Actual | Previous | Consensus | TEForecast
TE_CRUDE_HTML = """
<table>
<tr><th>Calendar</th><th>GMT</th><th>Reference</th><th>Actual</th>
    <th>Previous</th><th>Consensus</th><th>TEForecast</th></tr>
<tr><td>2026-09-16</td><td>02:30 PM</td><td>EIA Crude Oil Stocks Change</td>
    <td>Sep/11</td><td>-0.64M</td><td>-0.391M</td><td>-1.6M</td><td></td></tr>
<tr><td>2026-09-23</td><td>02:30 PM</td><td>EIA Crude Oil Stocks Change</td>
    <td>Sep/18</td><td>2.969M</td><td>-0.64M</td><td>-0.6M</td><td></td></tr>
<tr><td>2026-09-30</td><td>02:30 PM</td><td>EIA Crude Oil Stocks Change</td>
    <td>Sep/25</td><td></td><td>2.969M</td><td></td><td></td></tr>
</table>
"""

# Trading Economics API page: calendar table plus the undated summary table.
TE_API_HTML = """
<table>
<tr><td>2026-09-15</td><td>09:00 PM</td><td>API Crude Oil Stock Change</td>
    <td>Sep/11</td><td>7.14M</td><td>-0.3M</td><td>-1.8M</td><td></td></tr>
<tr><td>2026-09-22</td><td>09:00 PM</td><td>API Crude Oil Stock Change</td>
    <td>Sep/18</td><td>1.786M</td><td>7.14M</td><td>-0.5M</td><td></td></tr>
<tr><td>2026-09-29</td><td>08:30 PM</td><td>API Crude Oil Stock Change</td>
    <td>Sep/25</td><td></td><td>1.786M</td><td></td><td></td></tr>
</table>
<table>
<tr><td>API Crude Oil Stock Change</td><td>1.79</td><td>7.14</td>
    <td>BBL/1Million</td><td>Sep 2026</td></tr>
<tr><td>API Cushing Number</td><td>2.08</td><td>-0.25</td>
    <td>BBL/1Million</td><td>Sep 2026</td></tr>
<tr><td>API Distillate Stocks</td><td>-2.16</td><td>1.61</td>
    <td>BBL/1Million</td><td>Sep 2026</td></tr>
<tr><td>API Gasoline Stocks</td><td>-2.16</td><td>1.46</td>
    <td>BBL/1Million</td><td>Sep 2026</td></tr>
</table>
"""

# investing.com calendar service rows.
# Cells: time | currency | importance | event | actual | forecast | previous
INVESTING_HTML = """
<tr id="eventRowId_557509" data-event-datetime="2026/09/22 20:30:00">
  <td>20:30</td><td>&nbsp; USD</td><td></td><td>API Weekly Crude Oil Stock</td>
  <td>1.786M</td><td>-0.500M</td><td>7.140M</td><td></td></tr>
<tr id="eventRowId_557643" data-event-datetime="2026/09/23 14:30:00">
  <td>14:30</td><td>&nbsp; USD</td><td></td><td>Crude Oil Inventories</td>
  <td>2.969M</td><td>-0.700M</td><td>-0.640M</td><td></td></tr>
<tr id="eventRowId_557639" data-event-datetime="2026/09/23 14:30:00">
  <td>14:30</td><td>&nbsp; USD</td><td></td><td>Cushing Crude Oil Inventories</td>
  <td>2.266M</td><td>&nbsp;</td><td>-0.342M</td><td></td></tr>
<tr id="eventRowId_557647" data-event-datetime="2026/09/23 14:30:00">
  <td>14:30</td><td>&nbsp; USD</td><td></td><td>EIA Weekly Distillates Stocks</td>
  <td>-0.428M</td><td>-0.600M</td><td>1.585M</td><td></td></tr>
<tr id="eventRowId_557640" data-event-datetime="2026/09/23 14:30:00">
  <td>14:30</td><td>&nbsp; USD</td><td></td><td>Gasoline Inventories</td>
  <td>-1.686M</td><td>0.100M</td><td>0.794M</td><td></td></tr>
"""


# --- Trading Economics: value parsing --------------------------------------


@pytest.mark.parametrize(
    ("text", "unit", "expected"),
    [
        ("-0.64M", "", -0.64),
        ("2.969M", "", 2.969),
        ("-1M", "", -1.0),
        ("2.97", "BBL/1Million", 2.97),
        ("2266.00", "Thousand Barrels", 2.266),  # kb -> mb
        ("-342.00", "Thousand Barrels", -0.342),
        ("1,234", "Thousand Barrels", 1.234),  # thousands separator
        ("", "", None),
        ("n/a", "", None),
    ],
)
def test_te_parse_mb(text, unit, expected):
    assert te._parse_mb(text, unit) == expected


@pytest.mark.parametrize(
    ("reference", "release", "expected"),
    [
        ("Sep/11", "2026-09-16", date(2026, 9, 11)),
        ("Sep/25", "2026-09-30", date(2026, 9, 25)),
        ("Dec/26", "2027-01-02", date(2026, 12, 26)),  # year rolls back
        ("Jan/02", "2027-01-07", date(2027, 1, 2)),
        ("bogus", "2026-09-16", None),
        ("Xxx/11", "2026-09-16", None),
    ],
)
def test_te_reference_to_date(reference, release, expected):
    assert te._reference_to_date(reference, release) == expected


def test_te_calendar_keeps_blank_cells_in_position():
    """An empty Actual marks an unreleased row — dropping blanks shifts columns."""
    rows = te._calendar_rows(TE_CRUDE_HTML)
    assert len(rows) == 3

    released = rows[1]
    assert released["reference"] == "Sep/18"
    assert released["actual"] == 2.969
    assert released["previous"] == -0.64
    assert released["consensus"] == -0.6

    upcoming = rows[2]
    assert upcoming["reference"] == "Sep/25"
    assert upcoming["actual"] is None  # not the previous column
    assert upcoming["previous"] == 2.969
    assert upcoming["consensus"] is None


def test_te_consensus_reads_the_requested_week(monkeypatch):
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_CRUDE_HTML)
    result = te.fetch_consensus("2026-09-18")
    assert result == {
        "crude_consensus_mb": -0.6,
        "crude_previous_mb": -0.64,
        "gasoline_consensus_mb": -0.6,
        "distillate_consensus_mb": -0.6,
    }


def test_te_consensus_none_when_not_published(monkeypatch):
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_CRUDE_HTML)
    assert te.fetch_consensus("2026-09-25") is None


def test_te_consensus_none_for_an_unknown_week(monkeypatch):
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_CRUDE_HTML)
    assert te.fetch_consensus("2025-01-03") is None


def test_te_api_report_for_the_latest_released_week(monkeypatch):
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_API_HTML)
    assert te.fetch_api_report("2026-09-18") == {
        "api_crude_mb": 1.786,  # from the dated calendar row, not the summary
        "api_cushing_mb": 2.08,
        "api_gasoline_mb": -2.16,
        "api_distillate_mb": -2.16,
    }


def test_te_api_report_refuses_an_older_week(monkeypatch):
    """The summary table is undated, so an older week would get newer legs.

    Sep/11's crude actual is 7.14, but Cushing/gasoline/distillate in the
    summary belong to Sep/18. Serving them would be silently wrong.
    """
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_API_HTML)
    assert te.fetch_api_report("2026-09-11") is None


def test_te_api_report_none_before_release(monkeypatch):
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_API_HTML)
    assert te.fetch_api_report("2026-09-25") is None


# --- investing.com ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-0.640M", -0.64),
        ("2.969M", 2.969),
        ("0.100M", 0.1),
        ("141.1K", 0.1411),
        ("&nbsp;", None),
        ("", None),
    ],
)
def test_investing_parse_mb(text, expected):
    assert inv._parse_mb(text) == expected


def test_investing_rows_parse_with_exact_event_names():
    rows = inv._parse_service_html(INVESTING_HTML)
    by_event = {r["event"]: r for r in rows}
    assert len(rows) == 5

    # "Crude Oil Inventories" is a substring of "Cushing Crude Oil Inventories":
    # both must survive as distinct events.
    assert by_event["Crude Oil Inventories"]["forecast"] == -0.7
    assert by_event["Cushing Crude Oil Inventories"]["actual"] == 2.266
    assert by_event["Cushing Crude Oil Inventories"]["forecast"] is None
    assert by_event["Gasoline Inventories"]["forecast"] == 0.1
    assert by_event["EIA Weekly Distillates Stocks"]["forecast"] == -0.6
    assert by_event["Crude Oil Inventories"]["release_date"] == date(2026, 9, 23)


def test_investing_consensus_maps_release_to_reference_week(monkeypatch):
    """A Wednesday release covers the Friday before it."""
    monkeypatch.setattr(
        inv, "_calendar_rows", lambda *_: inv._parse_service_html(INVESTING_HTML)
    )
    assert inv.fetch_consensus("2026-09-18") == {
        "crude_consensus_mb": -0.7,
        "crude_previous_mb": -0.64,
        "gasoline_consensus_mb": 0.1,
        "distillate_consensus_mb": -0.6,
    }


def test_investing_consensus_none_for_a_week_it_has_no_rows_for(monkeypatch):
    monkeypatch.setattr(
        inv, "_calendar_rows", lambda *_: inv._parse_service_html(INVESTING_HTML)
    )
    assert inv.fetch_consensus("2026-09-25") is None


def test_investing_api_report_is_never_partial(monkeypatch):
    """Only the crude leg exists there, so it must not return a partial dict."""
    monkeypatch.setattr(
        inv, "_calendar_rows", lambda *_: inv._parse_service_html(INVESTING_HTML)
    )
    assert inv.fetch_api_report("2026-09-18") is None


def test_investing_api_crude_cross_check(monkeypatch):
    monkeypatch.setattr(
        inv, "_calendar_rows", lambda *_: inv._parse_service_html(INVESTING_HTML)
    )
    assert inv.fetch_api_crude_only("2026-09-18") == 1.786


# --- the two sources must not disagree on actuals -------------------------


def test_sources_agree_on_actuals(monkeypatch):
    """Captured the same day: actuals match exactly, consensus may not.

    Different survey panels, so crude consensus legitimately differs
    (TE -0.6 vs investing -0.7). Actuals are facts and must agree.
    """
    monkeypatch.setattr(te, "_fetch", lambda slug: TE_API_HTML)
    monkeypatch.setattr(
        inv, "_calendar_rows", lambda *_: inv._parse_service_html(INVESTING_HTML)
    )
    assert te.fetch_api_report("2026-09-18")["api_crude_mb"] == inv.fetch_api_crude_only(
        "2026-09-18"
    )
