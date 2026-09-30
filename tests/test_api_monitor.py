import datetime
import time

import pytest

from app.api_monitor import fetch_api_report

TODAY_AFTER = datetime.date(2026, 9, 23)   # the 22-09 report is due and printed
TODAY_BEFORE = datetime.date(2026, 9, 29)  # 29-09 report is due but not printed yet
RELEASED = "22-09-2026"


def rows(actual=1.786, released=True):
    out = [{"release_date": "15-09-2026", "time": "02:30 AM", "actual": 7.14, "consensus": -1.8, "previous": -0.3},
           {"release_date": RELEASED, "time": "02:30 AM", "actual": actual, "consensus": -0.5, "previous": 7.14}]
    if not released:
        out.append({"release_date": "29-09-2026", "time": "02:00 AM", "actual": None, "consensus": -1.9, "previous": 1.786})
    return out


class FakeScraper:
    session_gap_s = 0

    def __init__(self, page, delay):
        self.page, self.delay = page, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def fetch_page(self, slug):
        time.sleep(self.delay)
        return self.page


def make(**per_site):
    """per_site = {site: (page, delay)}; page None simulates a failed fetch."""
    return lambda site: FakeScraper(*per_site[site])


def site(rows_=None, delay=0):
    return ({"calendar_rows": rows_ if rows_ is not None else rows(), "stats": {}}, delay)


def test_crude_from_tradingeconomics_keeps_three_decimals():
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5,
                              make_scraper=make(tradingeconomics=site(), investing=site(delay=0.5)))
    assert result == {"release_date": RELEASED, "api_crude_mb": 1.786, "crude_source": "tradingeconomics"}


def test_the_report_is_crude_only():
    """Cushing/gasoline/distillate are paywalled or stale on TE: they must not reappear."""
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=site(), investing=site()))
    assert set(result) == {"release_date", "api_crude_mb", "crude_source"}


def test_a_faster_investing_wins():
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5,
                              make_scraper=make(tradingeconomics=site(delay=0.4), investing=site()))
    assert result["crude_source"] == "investing.com"


def test_one_site_down_the_other_still_delivers():
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5,
                              make_scraper=make(tradingeconomics=(None, 0), investing=site()))
    assert result["crude_source"] == "investing.com" and result["api_crude_mb"] == 1.786


def test_not_released_yet_is_an_error():
    r = rows(released=False)
    with pytest.raises(RuntimeError, match="not released yet"):
        fetch_api_report(today=TODAY_BEFORE, timeout_s=5, make_scraper=make(tradingeconomics=site(r), investing=site(r)))


def test_default_target_is_the_latest_due_release_not_next_weeks():
    r = rows(released=False)  # includes the upcoming 29-09 row
    result = fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=site(r), investing=site(r)))
    assert result["release_date"] == RELEASED


def test_an_explicit_older_date_is_now_fine_because_crude_is_a_dated_row():
    """Replaying an older release used to be rejected (the undated legs); crude alone can replay."""
    result = fetch_api_report("15-09-2026", today=TODAY_AFTER, timeout_s=5,
                              make_scraper=make(tradingeconomics=site(), investing=site()))
    assert (result["release_date"], result["api_crude_mb"]) == ("15-09-2026", 7.14)


def test_both_sites_down():
    with pytest.raises(RuntimeError, match="no valid API report value for crude"):
        fetch_api_report(today=TODAY_AFTER, timeout_s=5, make_scraper=make(tradingeconomics=(None, 0), investing=(None, 0)))


def test_stale_default_target_is_refused():
    """Holiday-shifted week: the latest due report is days old and already printed."""
    with pytest.raises(RuntimeError, match="days old"):
        fetch_api_report(today=datetime.date(2026, 9, 26), timeout_s=5,
                         make_scraper=make(tradingeconomics=site(), investing=site()))
