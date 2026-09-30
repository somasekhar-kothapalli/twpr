"""Where each indicator lives, per site. Edit this file to add or fix a URL.

A missing entry means that site does not carry (or we have not verified) the
indicator - callers skip it. Slugs are relative to the site's `base_url`.
tests/test_sources.py -m network checks every entry still returns data.
"""
from .sites.investing import InvestingCalendarScraper
from .sites.tradingeconomics import TradingEconomicsScraper

SITES = {
    "tradingeconomics": TradingEconomicsScraper,
    "investing": InvestingCalendarScraper,
}

# Names written into pipeline output files.
SOURCE_NAMES = {"tradingeconomics": "tradingeconomics", "investing": "investing.com"}

INDICATORS = {
    "eia_crude": {
        "tradingeconomics": "united-states/crude-oil-stocks-change",
        "investing": "eia-crude-oil-inventories-75",
    },
    "eia_gasoline": {
        "tradingeconomics": "united-states/gasoline-stocks-change",
        "investing": "weekly-gasoline-inventories-485",
    },
    "eia_distillate": {
        "tradingeconomics": "united-states/distillate-stocks",
        "investing": "eia-weekly-distillates-stocks-917",
    },
    "eia_cushing": {
        "tradingeconomics": "united-states/cushing-crude-oil-stocks",
        "investing": "eia-weekly-cushing-oil-inventories-1657",
    },
    "eia_refinery": {"investing": "eia-weekly-refinery-utilization-rates-1961"},
    "api_crude": {
        "tradingeconomics": "united-states/api-crude-oil-stock-change",
        "investing": "api-weekly-crude-stock-656",
    },
    # The API's cushing/gasoline/distillate are deliberately NOT here (paywalled at the source; the
    # tradingeconomics copies lag; investing.com's events for them are dead). Do not re-add.
}


def slug_for(indicator, site):
    """The slug for `indicator` on `site`, or None if that site doesn't carry it."""
    return INDICATORS[indicator].get(site)


def scraper_for(site, **kwargs):
    """A new scraper instance for `site` (use it as a context manager)."""
    return SITES[site](**kwargs)
