import datetime
import logging
import time

import pytest

from app.utils.common import poll
from app.eia_actuals import fetch_eia_actuals
from app.scraper.sources import slug_for
from app.scraper.utils.calendar import current_release

WED = "23-09-2026"
NEXT_WED = "30-09-2026"
TODAY = datetime.date(2026, 9, 23)  # release day
ACTUALS = {"crude": 2.969, "cushing": 2.266, "gasoline": -1.686, "distillate": -0.428}


def rows(actual, date=WED, upcoming=True, upcoming_date=NEXT_WED):
    out = [{"release_date": "16-09-2026", "time": "08:00 PM", "actual": -0.64, "consensus": -1.6, "previous": -0.391},
           {"release_date": date, "time": "08:00 PM", "actual": actual, "consensus": -0.6, "previous": -0.64}]
    if upcoming:
        out.append({"release_date": upcoming_date, "time": "08:00 PM", "actual": None, "consensus": None, "previous": actual})
    return out


def pages(site, overrides=None, **dates):
    """Canned pages for a site; overrides = {leg: actual} (None = not released)."""
    values = {**ACTUALS, **(overrides or {})}
    out = {slug_for(f"eia_{leg}", site): {"calendar_rows": rows(v, dates.get(leg, WED)), "stats": {}}
           for leg, v in values.items()}
    if slug_for("eia_refinery", site):  # investing.com carries the utilisation-change page
        out[slug_for("eia_refinery", site)] = {"calendar_rows": rows(-2.8), "stats": {}}
    return out


class FakeScraper:
    session_gap_s = 0

    def __init__(self, pages_, delay):
        self.pages, self.delay = pages_, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def fetch_page(self, slug):
        time.sleep(self.delay)
        return self.pages.get(slug)


def make(**per_site):
    return lambda site: FakeScraper(*per_site[site])


def run(release_date=None, sites=("tradingeconomics", "investing"), today=TODAY, **per_site):
    return fetch_eia_actuals(release_date, sites, make(**per_site), timeout_s=5, today=today)


def test_whole_report_from_the_faster_site():
    start = time.monotonic()
    result = run(tradingeconomics=(pages("tradingeconomics"), 0), investing=(pages("investing"), 0.6))
    assert time.monotonic() - start < 1.5  # TE done at once + the grace wait for investing's refinery page
    assert result == {
        "release_date": WED,
        "crude_change_mb": 2.969, "cushing_change_mb": 2.266,
        "gasoline_change_mb": -1.686, "distillate_change_mb": -0.428,
        "refinery_util_change_pct": -2.8,  # investing.com's, fetched first, arrived within the grace window
        "source": "tradingeconomics",
        "won_race": True,
    }


def test_a_faster_investing_wins_the_whole_report():
    result = run(tradingeconomics=(pages("tradingeconomics"), 0.4), investing=(pages("investing"), 0))
    assert result["source"] == "investing.com" and result["crude_change_mb"] == 2.969


def test_an_incomplete_site_is_not_a_candidate():
    te = pages("tradingeconomics", {"cushing": None})  # cushing not printed yet on TE
    result = run(tradingeconomics=(te, 0), investing=(pages("investing"), 0.2))
    assert result["source"] == "investing.com"  # never a mix of sites


def test_not_released_yet_fails_loudly():
    unreleased = {leg: None for leg in ACTUALS}
    with pytest.raises(RuntimeError, match="not released yet"):
        run(tradingeconomics=(pages("tradingeconomics", unreleased), 0), investing=(pages("investing", unreleased), 0))


def test_legs_for_different_releases_are_rejected():
    te = pages("tradingeconomics", gasoline="16-09-2026")  # gasoline row only exists for last week
    te[slug_for("eia_gasoline", "tradingeconomics")]["calendar_rows"] = rows(-1.686)[:1]
    with pytest.raises(RuntimeError, match="gasoline: no row for release"):
        run(sites=("tradingeconomics",), tradingeconomics=(te, 0))


def test_a_single_site_did_not_win_a_race():
    assert run(sites=("tradingeconomics",), tradingeconomics=(pages("tradingeconomics"), 0))["won_race"] is False


def test_explicit_date_replays_that_release():
    result = run("16-09-2026", tradingeconomics=(pages("tradingeconomics"), 0), investing=(pages("investing"), 0))
    assert result["release_date"] == "16-09-2026" and result["crude_change_mb"] == -0.64


def test_holiday_week_does_not_return_last_weeks_report():
    """Thursday-delayed release: on Wednesday the calendar's next row is dated
    Thursday, so the latest due row is last week's, already printed. It must be
    refused, not returned as fresh."""
    thursday_delay = "01-10-2026"
    held = {slug_for(f"eia_{leg}", site): {"calendar_rows": rows(v, upcoming_date=thursday_delay), "stats": {}}
            for site in ("tradingeconomics", "investing") for leg, v in ACTUALS.items()}
    with pytest.raises(RuntimeError, match="not on the calendar yet"):
        fetch_eia_actuals(None, ("tradingeconomics", "investing"), make(tradingeconomics=(held, 0), investing=(held, 0)),
                         timeout_s=5, today=datetime.date(2026, 9, 30))


def test_both_sites_down():
    with pytest.raises(RuntimeError, match="no valid EIA actuals"):
        run(tradingeconomics=({}, 0), investing=({}, 0))


def test_current_release_guard():
    r = rows(2.969, upcoming_date="01-10-2026")
    assert current_release(r, datetime.date(2026, 9, 23))["release_date"] == WED
    assert current_release(r, datetime.date(2026, 9, 24))["release_date"] == WED   # one day on: still current
    with pytest.raises(RuntimeError, match="7 days old"):
        current_release(r, datetime.date(2026, 9, 30))


def test_poll_retries_until_it_succeeds(caplog):
    calls = []

    def attempt():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("not yet")
        return "done"

    with caplog.at_level(logging.INFO):
        assert poll(attempt, once=False, interval_s=0, timeout_s=5, log=logging.getLogger("t")) == "done"
    assert len(calls) == 3


def test_poll_once_and_timeout_raise_and_other_errors_propagate():
    def never():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        poll(never, once=True, interval_s=0, timeout_s=5, log=logging.getLogger("t"))
    with pytest.raises(RuntimeError):
        poll(never, once=False, interval_s=10, timeout_s=1, log=logging.getLogger("t"))
    with pytest.raises(ValueError):
        poll(lambda: (_ for _ in ()).throw(ValueError("bug")), once=False, interval_s=0, timeout_s=5, log=logging.getLogger("t"))


def test_refinery_change_comes_from_investing_even_when_te_wins():
    result = fetch_eia_actuals(None, ("tradingeconomics", "investing"),
                              make(tradingeconomics=(pages("tradingeconomics"), 0.05), investing=(pages("investing"), 0.1)),
                              timeout_s=5, today=TODAY, grace_s=1)
    assert result["source"] == "tradingeconomics" and result["refinery_util_change_pct"] == -2.8
    assert "refinery_util_pct" not in result  # the level is not reported at all


def test_slow_refinery_is_null_and_does_not_delay_the_report():
    start = time.monotonic()
    result = fetch_eia_actuals(None, ("tradingeconomics", "investing"),
                              make(tradingeconomics=(pages("tradingeconomics"), 0), investing=(pages("investing"), 1.5)),
                              timeout_s=5, today=TODAY, grace_s=0.1)
    assert result["refinery_util_change_pct"] is None and result["source"] == "tradingeconomics"
    assert time.monotonic() - start < 1.0


def test_refinery_row_must_match_the_reports_release_date():
    inv = pages("investing")
    inv[slug_for("eia_refinery", "investing")]["calendar_rows"] = rows(-2.8, date="16-09-2026", upcoming=False)  # stale only
    result = fetch_eia_actuals(None, ("tradingeconomics", "investing"),
                              make(tradingeconomics=(pages("tradingeconomics"), 0), investing=(inv, 0)),
                              timeout_s=5, today=TODAY, grace_s=0.3)
    assert result["refinery_util_change_pct"] is None


def test_refinery_failure_never_sinks_the_report():
    inv = pages("investing")
    del inv[slug_for("eia_refinery", "investing")]  # refinery page fetch fails
    result = fetch_eia_actuals(None, ("investing",), make(investing=(inv, 0)), timeout_s=5, today=TODAY, grace_s=0.1)
    assert result["source"] == "investing.com" and result["refinery_util_change_pct"] is None


def test_only_tradingeconomics_has_no_refinery_change():
    result = run(sites=("tradingeconomics",), tradingeconomics=(pages("tradingeconomics"), 0))
    assert result["refinery_util_change_pct"] is None
