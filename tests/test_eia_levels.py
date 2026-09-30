import httpx
import pytest

from app.utils import eia_levels
from app.utils.eia_levels import cushing_level, parse_cushing_levels

# Shaped like eia.gov's table: nested markup, &nbsp;, thousand separators, trailing footnotes.
PAGE = """<table><tr><th>Show Data By:</th><th>09/04/26</th><th>09/11/26</th><th>09/18/26</th></tr>
<tr><td>Commercial Crude Oil (Excl. Lease Stock)</td><td>21,824</td><td>21,482</td><td>23,748</td></tr>
<tr><td>2004-2026 - = No Data Reported; Notes: Stocks include</td></tr></table>"""


def response(text, status=200):
    return httpx.Response(status, text=text, request=httpx.Request("GET", eia_levels.CUSHING_URL))


def getter(*pages):
    calls = iter(pages)
    return lambda *a, **k: next(calls)


def test_parse_reads_dates_and_thousand_barrels_as_mb():
    assert parse_cushing_levels(PAGE) == [("09/04/26", 21.824), ("09/11/26", 21.482), ("09/18/26", 23.748)]


def test_parse_rejects_a_changed_page():
    with pytest.raises(ValueError, match="unexpected Cushing table"):
        parse_cushing_levels("<p>nothing here</p>")


def test_level_returned_when_it_matches_the_reported_change():
    assert cushing_level(2.266, get=getter(response(PAGE)), sleep=lambda s: None) == 23.748


def test_last_weeks_page_is_not_returned_as_this_weeks(caplog):
    # EIA hasn't updated: the newest column's change (-0.342) is last week's, not the +2.266 we have.
    stale = PAGE.replace("<td>23,748</td>", "").replace("<th>09/18/26</th>", "")
    assert cushing_level(2.266, get=getter(response(stale)), tries=1, sleep=lambda s: None) is None
    assert "not updated yet" in caplog.text


def test_retries_until_the_page_updates():
    stale = PAGE.replace("<th>09/18/26</th>", "").replace("<td>23,748</td>", "")
    waits = []
    level = cushing_level(2.266, get=getter(response(stale), response(PAGE)), sleep=waits.append)
    assert level == 23.748 and waits == [20]


def test_http_error_or_network_failure_gives_none_not_a_crash():
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    assert cushing_level(2.266, get=boom, tries=2, sleep=lambda s: None) is None
    assert cushing_level(2.266, get=getter(response("x", 503)), tries=1, sleep=lambda s: None) is None
