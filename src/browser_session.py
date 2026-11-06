"""Shared Camoufox initialization for Playwright web sessions."""
from contextlib import contextmanager
from pathlib import Path


def launch_context(runtime, profile, *, headless=True):
    try:
        from camoufox.sync_api import NewBrowser
    except ImportError as error:
        raise RuntimeError('Install requirements-browser.txt and run: python -m camoufox fetch') from error
    return NewBrowser(runtime, persistent_context=True,
                      user_data_dir=str(Path(profile).resolve()),
                      headless=headless, accept_downloads=True, enable_cache=False)


@contextmanager
def open_search_browser(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        context = launch_context(runtime, '.acm-search-camoufox', headless=False)
        try:
            page = context.new_page()
            page.goto(url, wait_until='domcontentloaded')
            yield page
        finally:
            context.close()
