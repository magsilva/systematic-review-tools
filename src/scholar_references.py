"""Google Scholar lookup and citation export through a Playwright browser."""

from contextlib import ExitStack
import time
from urllib.parse import urlencode, urljoin, urlsplit

from bibtex_io import parse_bibtex
from browser_session import launch_context


def search_url(*, title='', author='', year='', citation='', doi=''):
    terms = ['"' + doi + '"'] if doi else (
        [f'"{title}"', author, year] if title else [citation])
    query = ' '.join(term for term in terms if term)
    return 'https://scholar.google.com/scholar?' + urlencode({'q': query}) if query else ''


class Scholar:
    """Match citations from supplied records or retrieve them automatically from Scholar."""

    def __init__(self, *, entries, logger, identify, normalize, online=False, timeout=30):
        self.entries = entries
        self.logger = logger
        self.identify = identify
        self.normalize = normalize
        self.cache = {}
        self.online = online
        self.timeout = int(timeout * 1000)
        self.stack = ExitStack()
        self.page = None
        self.next_search = 0

    def close(self):
        self.stack.close()

    def _browser(self):
        if self.page is None:
            from playwright.sync_api import sync_playwright
            runtime = self.stack.enter_context(sync_playwright())
            context = launch_context(runtime, '.scholar-search-camoufox')
            self.stack.callback(context.close)
            self.page = context.new_page()
        return self.page

    def _search(self, url, *, title, citation, doi):
        page = self._browser()
        time.sleep(max(0, self.next_search - time.monotonic()))
        self.next_search = time.monotonic() + 3
        response = page.goto(url, wait_until='domcontentloaded', timeout=self.timeout)
        if (response is None or response.status >= 400
                or '/sorry/' in page.url
                or page.locator('form#captcha-form, iframe[src*="recaptcha"]').count()):
            raise RuntimeError('Scholar search unavailable or requires verification')
        if urlsplit(page.url).hostname != 'scholar.google.com':
            raise RuntimeError('Unexpected Scholar search destination')

        records = []
        results = page.locator('.gs_r.gs_or.gs_scl')
        for index in range(min(5, results.count())):
            result = results.nth(index)
            heading = result.locator('.gs_rt')
            if not heading.count():
                continue
            candidate = self.normalize(heading.inner_text(timeout=self.timeout))
            candidate = candidate.removeprefix('citation ').removeprefix('pdf ').removeprefix('html ')
            link = heading.locator('a')
            href = link.first.get_attribute('href', timeout=self.timeout) if link.count() else ''
            target = urlsplit(href or '')
            inferred_doi = ''
            if target.hostname in ('doi.org', 'dx.doi.org'):
                inferred_doi = self.identify({'url': href})
            if not ((doi and doi == inferred_doi)
                    or (title and self.normalize(title) == candidate)
                    or (not title and citation and len(candidate.split()) >= 3
                        and ' ' + candidate + ' ' in ' ' + self.normalize(citation) + ' ')):
                continue
            cite = result.locator('.gs_or_cit')
            if not cite.count():
                continue
            cite.click(timeout=self.timeout)
            try:
                bibtex = page.get_by_role('link', name='BibTeX', exact=True)
                export = urljoin(page.url, bibtex.get_attribute('href', timeout=self.timeout) or '')
                target = urlsplit(export)
                if (target.scheme != 'https' or target.hostname not in
                        ('scholar.google.com', 'scholar.googleusercontent.com')
                        or target.path != '/scholar.bib'):
                    raise RuntimeError('Unexpected Scholar citation export link')
                reply = page.request.get(export, timeout=self.timeout, max_redirects=0)
                try:
                    if not reply.ok:
                        raise RuntimeError('Scholar citation export returned HTTP %s' % reply.status)
                    exported = parse_bibtex(reply.text(), 'Google Scholar citation').entries
                finally:
                    reply.dispose()
                if len(exported) == 1:
                    entry = exported[0]
                    if inferred_doi:
                        entry.setdefault('doi', inferred_doi)
                    records.append(entry)
            finally:
                page.keyboard.press('Escape')
        return records


    def find(self, *, title='', author='', year='', citation='', doi=''):
        url = search_url(title=title, author=author, year=year, citation=citation, doi=doi)
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        matches = self._matches(self.entries, title=title, author=author,
                                year=year, citation=citation, doi=doi)
        if not matches and self.online:
            try:
                records = self._search(url, title=title, citation=citation, doi=doi)
                matches = self._matches(records, title=title, author=author,
                                        year=year, citation=citation, doi=doi)
            except Exception as error:
                self.online = False
                self.logger.warning('Stopping automated Scholar lookups: %s', error)
        result = matches[0] if len(matches) == 1 else None
        if result is None:
            self.logger.info('Google Scholar lookup: %s', url)
        self.cache[url] = result
        return result

    def _matches(self, entries, *, title, author, year, citation, doi):
        matches = []
        for entry in entries:
            candidate_doi = self.identify(entry)
            if doi and candidate_doi:
                if doi == candidate_doi:
                    matches.append(entry)
                continue
            candidate_title = self.normalize(entry.get('title', ''))
            first_author = entry.get('author', '').split(' and ')[0]
            candidate_author = self.normalize(first_author.split(',', 1)[0] if ',' in first_author
                                              else first_author).split()
            family = candidate_author[-1] if candidate_author else ''
            candidate_year = entry.get('year', '')
            if not candidate_title or (year and candidate_year != year):
                continue
            if title:
                if candidate_title != self.normalize(title):
                    continue
                # Require corroboration for batch matching, not just a title.
                if author:
                    parts = self.normalize(author).split()
                    if not family or family not in parts:
                        continue
                elif not year:
                    continue
            else:
                text = ' ' + self.normalize(citation) + ' '
                if (len(candidate_title.split()) < 3 or ' ' + candidate_title + ' ' not in text
                        or not candidate_year or ' ' + candidate_year + ' ' not in text
                        or not family or ' ' + family + ' ' not in text):
                    continue
            matches.append(entry)

        return matches
