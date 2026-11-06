"""Lazy, visible Playwright browser transport for ACM PDF endpoints."""

from pathlib import Path


class BrowserPdfDownloader:
    """Keep one isolated persistent browser session for an entire download queue."""

    def __init__(self, profile):
        self.profile = Path(profile)
        self.runtime = None
        self.context = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.runtime is not None:
                self.runtime.stop()

    def download(self, url, timeout, max_bytes):
        if self.runtime is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as error:
                raise RuntimeError('ACM requires a browser. Install requirements-browser.txt '
                                   'and run: python -m playwright install chromium') from error
            self.runtime = sync_playwright().start()
            self.context = self.runtime.chromium.launch_persistent_context(
                str(self.profile.resolve()), headless=False, accept_downloads=True)
        if self.context is None:
            raise RuntimeError('ACM browser could not be started.')
        page = self.context.new_page()
        downloads = []
        page.on('download', downloads.append)

        def restrict_navigation(route):
            request = route.request
            if request.is_navigation_request() and request.url != url:
                route.abort()
            else:
                route.continue_()

        page.route('**/*', restrict_navigation)
        try:
            response = None
            navigation_error = None
            try:
                response = page.goto(url, wait_until='load', timeout=timeout*1000)
            except Exception as error:
                # An attachment aborts navigation but emits a browser download event.
                navigation_error = error
            if downloads:
                download = downloads[0]
                if download.url != url:
                    raise ValueError('ACM browser download used an unexpected URL.')
                path = Path(download.path())
                if path.stat().st_size > max_bytes:
                    raise ValueError('PDF exceeds the configured size limit.')
                return path.read_bytes()
            if navigation_error is not None:
                raise navigation_error
            if response is None or response.url != url or response.status != 200:
                status = response.status if response is not None else 'no response'
                raise ValueError('ACM browser returned %s. Browser login or security '
                                 'verification may be required.' % status)
            length = response.headers.get('content-length')
            if length and int(length) > max_bytes:
                raise ValueError('PDF exceeds the configured size limit.')
            body = response.body()
            if len(body) > max_bytes:
                raise ValueError('PDF exceeds the configured size limit.')
            return body
        finally:
            page.close()
