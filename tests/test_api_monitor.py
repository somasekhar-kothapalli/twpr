import datetime
import time

import pytest
from bs4 import BeautifulSoup

from app.api_monitor import RELATED_NAMES, fetch_api_report, legs_from_snapshot
from app.scraper.sites.tradingeconomics import parse_related_table
from app.scraper.sources import slug_for

TODAY_AFTER = datetime.date(2026, 9, 23)   # the 22-09 report is due and printed
TODAY_BEFORE = datetime.date(2026, 9, 29)  # 29-09 report is due but not printed yet
RELEASED = "22-09-2026"


def rows(actual=1.786, released=True):
    out = [{"release_date": "15-09-2026", "time": "02:30 AM", "actual": 7.14, "consensus": -1.8, "previous": -0.3},
           {"release_date": RELEASED, "time": "02:30 AM", "actual": actual, "consensus": -0.5, "previous": 7.14}]
    if not released:
        out.append({"release_date": "29-09-2026", "time": "02:00 AM", "actual": None, "consensus": -1.9, "previous": 1.786})
    return out


def related_soup(crude=1.79, cushing=2.08, gasoline=-2.16, distillate=-2.16):
    values = {"crude": crude, "cushing": cushing, "gasoline": gasoline, "distillate": distillate}
    body = "".join(
        f"<tr><td>{RELATED_NAMES[k]}</td><td>{v}</td><td>0.5</td><td>BBL/1Million</td><td>Sep 2026</td></tr>"
        for k, v in values.items() if v is not None)
    return BeautifulSoup(
        '<table class="table"><thead><tr><th>Related</th><th>Last</th><th>Previous</th><th>Unit</th>'
        f"<th>Reference</th></tr></thead><tbody>{body}</tbody></table>", "html.parser")


class FakeScraper:
    session_gap_s = 0

    def __init__(self, result, delay):
        self.result, self.delay = result, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def fetch_with_soup(self, slug):
        time.sleep(self.delay)
        return self.result


def make(**per_site):
    """per_site = {site: ((page, soup), delay)}; page None simulates a failed fetch."""
    return lambda site: FakeScraper(*per_site[site])


def te(rows_=None, soup=None, delay=0):
    page = {"calendar_rows": rows_ if rows_ is not None else rows(), "stats": {}}
    return ((page, soup or related_soup()), delay)


def inv(rows_=None, delay=0):
    return (({"calendar_rows": rows_ if rows_ is not None else rows(), "stats": {}}, None), delay)


def run(**per_site):
    kwargs = {"today": TODAY_AFTER, "make_scraper": make(**per_site), "timeout_s": 5}
    kwargs.update(per_site.pop("_kw", {}))
    return fetch_api_report(**kwargs)


def test_all_four_legs_from_tradingeconomics():
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=te(), investing=inv(delay=0.5)))
    assert result == {
        "release_date": RELEASED,
        "api_crude_mb": 1.786,           # dated row keeps 3 decimals, not the snapshot's 1.79
        "api_cushing_mb": 2.08,
        "api_gasoline_mb": -2.16,
        "api_distillate_mb": -2.16,
        "crude_source": "tradingeconomics",
        "cushing_source": "tradingeconomics",
        "gasoline_source": "tradingeconomics",
        "distillate_source": "tradingeconomics",
    }


def test_a_faster_investing_wins_crude_only():
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5,
                              make_scraper=make(tradingeconomics=te(delay=0.4), investing=inv()))
    assert result["crude_source"] == "investing.com"
    assert {result["cushing_source"], result["gasoline_source"], result["distillate_source"]} == {"tradingeconomics"}


def test_stale_related_snapshot_is_rejected():
    """TE updated the dated crude row but the undated legs still show last week."""
    stale = related_soup(crude=7.14, cushing=-0.25, gasoline=1.46, distillate=1.61)
    with pytest.raises(RuntimeError, match="different release") as err:
        fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=te(soup=stale), investing=inv()))
    for leg in ("cushing", "gasoline", "distillate"):
        assert leg in str(err.value)
    assert "crude (" not in str(err.value)  # crude itself was fine


def test_not_released_yet_is_an_error():
    r = rows(released=False)
    r[-1]["actual"] = None
    with pytest.raises(RuntimeError, match="not released yet"):
        fetch_api_report(today=TODAY_BEFORE, timeout_s=5, make_scraper=make(tradingeconomics=te(rows_=r), investing=inv(rows_=r)))


def test_default_target_is_the_latest_due_release_not_next_weeks():
    r = rows(released=False)  # includes the upcoming 29-09 row
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=te(rows_=r), investing=inv(rows_=r)))
    assert result["release_date"] == RELEASED


def test_investing_alone_cannot_supply_the_legs():
    with pytest.raises(RuntimeError, match=r"no valid API report value for cushing \(no site supplied it\), gasoline"):
        fetch_api_report(today=TODAY_AFTER, sites=("investing",), timeout_s=5, make_scraper=make(investing=inv()))


def test_a_leg_missing_from_the_snapshot_fails_only_that_leg():
    with pytest.raises(RuntimeError, match="gasoline") as err:
        fetch_api_report(today=TODAY_AFTER, timeout_s=5,
                         make_scraper=make(tradingeconomics=te(soup=related_soup(gasoline=None)), investing=inv()))
    assert "cushing (" not in str(err.value) and "distillate (" not in str(err.value)


def test_explicit_older_date_cannot_use_the_latest_only_snapshot():
    older = "15-09-2026"
    with pytest.raises(RuntimeError, match="different release"):
        fetch_api_report(older, today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=te(), investing=inv()))


def test_both_sites_down():
    with pytest.raises(RuntimeError, match="no valid API report value for crude"):
        fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=((None, None), 0), investing=((None, None), 0)))


def test_related_table_parser_and_snapshot_check():
    related = parse_related_table(related_soup())
    assert related[RELATED_NAMES["cushing"]]["actual_mb"] == 2.08
    ok = legs_from_snapshot(related, {"actual": 1.786})
    assert ok == {"cushing": 2.08, "gasoline": -2.16, "distillate": -2.16}
    assert all(isinstance(v, ValueError) for v in legs_from_snapshot(related, {"actual": 5.0}).values())
    assert parse_related_table(BeautifulSoup("<p>nothing</p>", "html.parser")) == {}


def test_stale_default_target_is_refused():
    """Holiday-shifted week: the latest due report is days old and already printed."""
    with pytest.raises(RuntimeError, match="days old"):
        fetch_api_report(today=datetime.date(2026, 9, 26), timeout_s=5,
                         make_scraper=make(tradingeconomics=te(), investing=inv()))
