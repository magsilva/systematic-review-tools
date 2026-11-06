"""Lazy browser transport for direct PDFs and publisher landing pages."""

from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit


class PdfBytes(bytes):
    """PDF payload carrying the final browser download URL for provenance."""
    def __new__(cls, data, url):
        result = super().__new__(cls, data)
        result.url = url
        return result


class BrowserPdfDownloader:
    """Keep one isolated persistent browser session for an entire download queue."""

    def __init__(self, profile, *, headless=True):
        self.profile = Path(profile)
        self.headless = headless
        self.runtime = None
        self.context = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            if self.context is not None:
                try:
                    self.context.close()
                except Exception:
                    if args[0] is None:
                        raise
        finally:
            if self.runtime is not None:
                try:
                    self.runtime.stop()
                except Exception:
                    if args[0] is None:
                        raise

    def download(self, url, timeout, *, strict=True, limiter=None, progress=None):
        if self.runtime is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as error:
                raise RuntimeError('PDF retrieval requires a browser. Install requirements-browser.txt '
                                   'and run: python -m playwright install chromium') from error
            self.runtime = sync_playwright().start()
            self.context = self.runtime.chromium.launch_persistent_context(
                str(self.profile.resolve()), headless=self.headless, accept_downloads=True)
        if self.context is None:
            raise RuntimeError('Download browser could not be started.')
        page = self.context.new_page()
        downloads = []
        page.on('download', lambda download: downloads.append(download))

        def restrict_navigation(route):
            request = route.request
            if request.is_navigation_request() and strict and request.url != url:
                route.abort()
            else:
                route.continue_()

        page.route('**/*', restrict_navigation)
        try:
            pending, visited = [url], set()
            last_error = None
            while pending and len(visited) < 10:
                target = pending.pop(0)
                if target in visited or urlsplit(target).scheme not in ('http', 'https'):
                    continue
                if visited and limiter is not None:
                    delay = limiter.retry_delay if last_error is not None else limiter.delay
                    while delay(target):
                        time.sleep(min(delay(target), 60))
                    limiter.started(target)
                visited.add(target)
                if progress is not None:
                    progress.browser_url(progress.entry, target)
                downloads.clear()
                try:
                    response = None
                    navigation_error = None
                    try:
                        response = page.goto(target, wait_until='load', timeout=timeout*1000)
                    except Exception as error:
                        # Attachments abort navigation but emit a browser download event.
                        navigation_error = error
                    if downloads:
                        download = downloads[0]
                        if strict and download.url != url:
                            raise ValueError('ACM browser download used an unexpected URL.')
                        if limiter is not None and download.url != target:
                            limiter.started(download.url)
                        path = Path(download.path())
                        return PdfBytes(path.read_bytes(), download.url)
                    if navigation_error is not None:
                        raise navigation_error
                    if response is None or (strict and response.url != url):
                        raise ValueError('Browser returned no response or an unexpected URL.')
                    if limiter is not None and response.url != target:
                        limiter.started(response.url)
                    if response.status != 200:
                        raise ValueError('Browser returned HTTP %s. Login or security '
                                         'verification may be required.' % response.status)
                    if strict or 'application/pdf' in response.headers.get('content-type', '').lower():
                        body = response.body()
                        return PdfBytes(body, response.url)
                    # Some servers omit the PDF content type. Inspect the signature first.
                    body = response.body()
                    if b'%PDF-' in body[:1024]:
                        return PdfBytes(body, response.url)
                    if len(body) > 2*1024*1024:
                        raise ValueError('HTML page exceeds the 2 MiB discovery limit.')
                    links = page.locator('meta[name="citation_pdf_url"], a[href], link[href]').evaluate_all(
                        """els => els.map(e => e.tagName === 'META' ? e.content :
                            ((e.type === 'application/pdf' || /\\.pdf(?:[?#]|$)/i.test(e.href))
                            ? e.href : null)).filter(Boolean)""")
                    if not links:
                        raise ValueError('Browser page advertises no PDF link.')
                    pending.extend(urljoin(page.url, link) for link in links[:10])
                    last_error = None
                except Exception as error:
                    last_error = error
            raise ValueError('Browser could not retrieve a PDF: %s' %
                             (last_error or 'discovery limit reached'))
        finally:
            page.close()
