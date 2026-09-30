from contextlib import contextmanager
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

log = logging.getLogger("twpr.te_scraper")

RELOAD_RETRIES = 2


@contextmanager
def browser_session():
    """Launch one headless chromium browser + page, reusable across multiple .goto() calls.

    Use this instead of render_page/render_html when a scrape needs several page loads
    (e.g. paginated lists) — spinning up a fresh browser per page is wasteful.
    """
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            slow_mo=150,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )
        page = ctx.new_page()
        log.debug("Browser opened")
        try:
            yield page
        finally:
            browser.close()


def goto(page, url: str, wait_selector: str | None = None, timeout_ms: int = 20000, required: bool = True) -> None:
    """Navigate an existing page to url and let JS run.

    Uses domcontentloaded rather than networkidle: sites with ads/analytics/trackers
    often never go fully network-idle, and wait_selector is the real readiness signal.
    If wait_selector is given and required=False, a missing selector (e.g. a paginated
    list page past the last real page) is tolerated instead of raising.
    """
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    # investing.com answers a fresh session with 403 and lets a reload through.
    for _ in range(RELOAD_RETRIES):
        if response is None or response.status < 400:
            break
        page.wait_for_timeout(5000)
        response = page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    if response is not None and response.status >= 400:
        raise RuntimeError(f"{url} returned HTTP {response.status} (blocked or rate-limited)")
    if wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=timeout_ms)
        except PlaywrightTimeoutError:
            if required:
                raise


@contextmanager
def render_page(url: str, wait_selector: str | None = None, timeout_ms: int = 20000):
    """Load url in headless chromium, let JS run, yield the Playwright page."""
    with browser_session() as page:
        goto(page, url, wait_selector, timeout_ms)
        yield page


def render_html(url: str, wait_selector: str | None = None, timeout_ms: int = 20000) -> str:
    with render_page(url, wait_selector, timeout_ms) as page:
        return page.content()