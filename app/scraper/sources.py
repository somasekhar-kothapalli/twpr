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
    # investing.com's API cushing/gasoline/distillate events (1656/657/1035) are dead:
    # newest rows are 2016/2022, served without error. Do not re-add them.
    "api_cushing": {"tradingeconomics": "united-states/api-cushing-number"},
    "api_gasoline": {"tradingeconomics": "united-states/api-gasoline-stocks"},
    "api_distillate": {"tradingeconomics": "united-states/api-distillate-stocks"},
}


def slug_for(indicator, site):
    """The slug for `indicator` on `site`, or None if that site doesn't carry it."""
    return INDICATORS[indicator].get(site)


def scraper_for(site, **kwargs):
    """A new scraper instance for `site` (use it as a context manager)."""
    return SITES[site](**kwargs)
