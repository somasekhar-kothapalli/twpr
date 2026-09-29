import datetime

import pytest

from app.scraper.sources import INDICATORS, SITES, scraper_for, slug_for
from app.scraper.utils.calendar import latest_released_row, parse_date

MAX_STALE_DAYS = 21  # weekly reports; a dead event serves years-old rows without any error

ENTRIES = [(ind, site, slug) for ind, sites in INDICATORS.items() for site, slug in sites.items()]


def test_registry_is_well_formed():
    for indicator, sites in INDICATORS.items():
        for site, slug in sites.items():
            assert site in SITES, f"{indicator}: unknown site {site!r}"
            assert slug and not slug.startswith("/"), f"{indicator}/{site}: bad slug {slug!r}"


def test_slug_for_missing_site_is_none():
    assert slug_for("eia_crude", "investing")
    assert slug_for("eia_refinery", "tradingeconomics") is None


@pytest.mark.network
@pytest.mark.parametrize("indicator,site,slug", ENTRIES, ids=[f"{i}-{s}" for i, s, _ in ENTRIES])
def test_slug_still_returns_data(indicator, site, slug):
    """URL-rot detector: every registered slug must still yield a parsed page.
    investing.com rate-limits, so run it sparingly and one slug per session."""
    with scraper_for(site) as scraper:
        page = scraper.fetch_page(slug)
    assert page is not None, f"{site}/{slug}: fetch failed (blocked, moved, or markup changed)"
    assert page["calendar_rows"] or any(v is not None for v in page["stats"].values()), (
        f"{site}/{slug}: page loaded but nothing parsed"
    )
    latest = latest_released_row(page["calendar_rows"])
    if latest:
        age = (datetime.date.today() - parse_date(latest["release_date"])).days
        assert age <= MAX_STALE_DAYS, (
            f"{site}/{slug}: newest released row is {latest['release_date']} ({age} days old) - dead event?"
        )
