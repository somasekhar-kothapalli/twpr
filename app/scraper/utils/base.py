"""Shared scaffolding for the calendar scrapers, so both sites return the same
response by construction. A site subclass only supplies where to fetch and how
to parse; everything about the response shape lives here."""
import logging
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..browser import PlaywrightTimeoutError, browser_session, goto, render_html
from .calendar import row_for_release

log = logging.getLogger("twpr.scraper")

STAT_KEYS = ("actual_mb", "forecast_mb", "previous_mb")


class CalendarScraper:
    """fetch_page(slug) -> {"calendar_rows": [row, ...] | None,
                            "stats": {actual_mb, forecast_mb, previous_mb}}
    or None on any fetch/render failure. Rows are
    {release_date (DD-MM-YYYY), time, actual, forecast, previous}. `stats` always carries all
    three keys (None when a site lacks one), and calendar_rows is None - never
    [] - when the page has no rows.

    Use as a context manager to reuse one browser across several slugs.
    Subclasses set base_url / wait_selector / page_gap_s and implement
    parse_rows(soup) and parse_stats(soup, rows).
    """

    base_url = ""
    wait_selector = ""
    page_gap_s = 0  # minimum seconds between page loads; sites that rate-limit set this

    def __init__(self, timeout_ms=30000, page_gap_s=None):
        self.timeout_ms = timeout_ms
        if page_gap_s is not None:
            self.page_gap_s = page_gap_s
        self._page = None
        self._session_cm = None
        self._last_load = None

    def __enter__(self):
        self._session_cm = browser_session()
        self._page = self._session_cm.__enter__()
        return self

    def __exit__(self, *exc_info):
        if self._session_cm is not None:
            self._session_cm.__exit__(*exc_info)
        self._page = None
        self._session_cm = None

    def fetch_page(self, slug):
        url = urljoin(self.base_url, slug.lstrip("/"))
        name = type(self).__name__
        try:
            html = self._render(url)
        except PlaywrightTimeoutError:
            log.warning(f"{name}: no table on {url} within {self.timeout_ms}ms (bad slug or blocked)")
            return None
        except Exception:
            log.exception(f"{name}: page render failed for slug={slug}")
            return None

        soup = BeautifulSoup(html, "html.parser")
        rows = self.parse_rows(soup)
        stats = self.parse_stats(soup, rows)
        return {
            "calendar_rows": rows or None,
            "stats": {key: stats.get(key) for key in STAT_KEYS},
        }

    def fetch_release(self, slug, release_date=None):
        """The calendar row released on `release_date` (DD-MM-YYYY; default:
        latest released), or None."""
        page = self.fetch_page(slug)
        return row_for_release(page["calendar_rows"], release_date) if page else None

    def _render(self, url):
        if self._last_load is not None:
            wait = self.page_gap_s - (time.monotonic() - self._last_load)
            if wait > 0:
                time.sleep(wait)
        try:
            if self._page is not None:
                goto(self._page, url, wait_selector=self.wait_selector, timeout_ms=self.timeout_ms)
                return self._page.content()
            return render_html(url, wait_selector=self.wait_selector, timeout_ms=self.timeout_ms)
        finally:
            self._last_load = time.monotonic()

    def parse_rows(self, soup):
        raise NotImplementedError

    def parse_stats(self, soup, rows):
        raise NotImplementedError
