import time

import pytest

from app.consensus_fetcher import INDICATORS, fetch_consensus
from app.racer import decide
from app.scraper.sources import slug_for

UPCOMING = "30-09-2026"
RELEASED = "23-09-2026"


def rows(consensus, previous=2.969, pending_date=UPCOMING):
    """A released row (last week) plus the pending one this week."""
    return [
        {"release_date": RELEASED, "time": "08:00 PM", "actual": 2.969, "consensus": -0.7, "previous": -0.64},
        {"release_date": pending_date, "time": "08:00 PM", "actual": None, "consensus": consensus, "previous": previous},
    ]


def pages(site, crude=-0.6, gasoline=0.1, distillate=-0.5, previous=2.969, dates=None):
    """Canned pages for one site; a None consensus means 'not posted yet'."""
    dates = dates or {}
    return {
        slug_for(f"eia_{name}", site): {
            "calendar_rows": rows(value, previous, dates.get(name, UPCOMING)),
            "stats": {},
        }
        for name, value in (("crude", crude), ("gasoline", gasoline), ("distillate", distillate))
    }


class FakeScraper:
    session_gap_s = 0

    def __init__(self, pages, delay):
        self.pages, self.delay = pages, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def fetch_page(self, slug):
        time.sleep(self.delay)
        return self.pages.get(slug)


def make(**per_site):
    """make_scraper stand-in: per_site = {site: (pages, delay_seconds)}."""
    return lambda site: FakeScraper(*per_site[site])


TE_FAST = dict(tradingeconomics=(pages("tradingeconomics"), 0), investing=(pages("investing", crude=-0.7), 0.6))


def test_everything_from_the_faster_site_without_waiting_for_the_slower():
    start = time.monotonic()
    result = fetch_consensus(make_scraper=make(**TE_FAST))
    assert time.monotonic() - start < 0.5
    assert result == {
        "release_date": UPCOMING,
        "crude_consensus_mb": -0.6,
        "gasoline_consensus_mb": 0.1,
        "distillate_consensus_mb": -0.5,
        "crude_previous_mb": 2.969,
        "crude_source": "tradingeconomics",
        "gasoline_source": "tradingeconomics",
        "distillate_source": "tradingeconomics",
    }


def test_sources_are_recorded_per_indicator():
    te = pages("tradingeconomics", distillate=None)  # TE hasn't posted distillate yet
    inv = pages("investing", crude=-0.7, gasoline=0.2, distillate=-0.4)
    result = fetch_consensus(make_scraper=make(tradingeconomics=(te, 0), investing=(inv, 0.2)))
    assert (result["crude_source"], result["gasoline_source"], result["distillate_source"]) == (
        "tradingeconomics", "tradingeconomics", "investing.com")
    assert result["distillate_consensus_mb"] == -0.4
    assert result["crude_consensus_mb"] == -0.6 and result["gasoline_consensus_mb"] == 0.1


def test_a_faster_fallback_wins_that_indicator():
    slow_te = (pages("tradingeconomics"), 0.5)
    fast_inv = (pages("investing", crude=-0.7, gasoline=0.2, distillate=-0.4), 0)
    result = fetch_consensus(make_scraper=make(tradingeconomics=slow_te, investing=fast_inv))
    assert {result["crude_source"], result["gasoline_source"], result["distillate_source"]} == {"investing.com"}


def test_crude_previous_travels_with_the_crude_winner():
    te = pages("tradingeconomics", previous=2.969)
    inv = pages("investing", previous=2.5)
    result = fetch_consensus(make_scraper=make(tradingeconomics=(te, 0.3), investing=(inv, 0)))
    assert result["crude_source"] == "investing.com" and result["crude_previous_mb"] == 2.5


def test_crude_without_previous_is_rejected():
    te = pages("tradingeconomics", previous=None)
    result = fetch_consensus(make_scraper=make(tradingeconomics=(te, 0), investing=(pages("investing", previous=2.4), 0.2)))
    assert result["crude_source"] == "investing.com" and result["crude_previous_mb"] == 2.4


def test_explicit_date_selects_that_release():
    result = fetch_consensus(RELEASED, make_scraper=make(**TE_FAST))
    assert result["release_date"] == RELEASED
    assert result["crude_consensus_mb"] == -0.7


def test_a_candidate_for_a_different_release_is_skipped():
    te = pages("tradingeconomics", dates={"gasoline": "07-10-2026"})  # gasoline row from another week
    result = fetch_consensus(make_scraper=make(tradingeconomics=(te, 0), investing=(pages("investing", gasoline=0.3), 0.2)))
    assert result["release_date"] == UPCOMING
    assert result["gasoline_source"] == "investing.com" and result["gasoline_consensus_mb"] == 0.3


def test_fails_loudly_naming_the_indicator_nobody_could_deliver():
    te = pages("tradingeconomics", distillate=None)
    inv = pages("investing", distillate=None)
    with pytest.raises(RuntimeError, match=r"no valid consensus for distillate") as err:
        fetch_consensus(make_scraper=make(tradingeconomics=(te, 0), investing=(inv, 0)))
    assert "not posted yet" in str(err.value) and "crude" not in str(err.value)


def test_fails_loudly_when_both_sites_are_down():
    with pytest.raises(RuntimeError, match="no valid consensus for crude") as err:
        fetch_consensus(make_scraper=make(tradingeconomics=({}, 0), investing=({}, 0)))
    assert "gasoline" in str(err.value) and "distillate" in str(err.value)


def test_no_upcoming_release_is_an_error_not_a_guess():
    only_released = {slug_for(f"eia_{n}", s): {"calendar_rows": rows(0.1)[:1], "stats": {}}
                     for s in ("tradingeconomics", "investing") for n in ("crude", "gasoline", "distillate")}
    with pytest.raises(RuntimeError, match="no row for release"):
        fetch_consensus(make_scraper=make(tradingeconomics=(only_released, 0), investing=(only_released, 0)))


def test_timeout_when_nobody_answers():
    slow = make(tradingeconomics=(pages("tradingeconomics"), 2), investing=(pages("investing"), 2))
    with pytest.raises(RuntimeError, match="no answer within"):
        fetch_consensus(make_scraper=slow, timeout_s=0.3)


def test_decide_holds_gasoline_until_the_crude_date_is_known():
    cand = lambda d: {"release_date": d, "consensus": 0.1, "previous": 1.0}
    assert decide(INDICATORS, {"gasoline": [("tradingeconomics", cand(UPCOMING))]}) == {}
    both = {"crude": [("investing", cand(UPCOMING))], "gasoline": [("tradingeconomics", cand(UPCOMING))]}
    assert set(decide(INDICATORS, both)) == {"crude", "gasoline"}
