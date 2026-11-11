#!/usr/bin/env python3
"""Search SBC's SOL using Playwright/Camoufox and export enriched BibTeX."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
import sys
from urllib.parse import urlencode, urljoin

from bibtex_io import escape_bibtex, merge_sources, new_database, parse_bibtex, write_bibtex
from browser_session import launch_context
from merge_bibtex import merge_entries

SEARCH_URL = 'https://sol.sbc.org.br/busca/index.php/integrada/results'
CITATION_SELECTOR = ('a[href*="BibtexCitationPlugin"], a[href*="format=bibtex"], '
                     'a[href*="/citationstylelanguage/get/bibtex"]')


def build_search_url(query, page=1):
    """Search all three collections, preserving the canonical Boolean query."""
    if not query.strip():
        raise ValueError('The search query must not be empty.')
    return SEARCH_URL + '?' + urlencode([
        ('isAdvanced', 1), ('query', query.strip()), ('searchPage', page),
        *[('archiveIds[]', number) for number in (1, 2, 3)]])


def visit(page, url, timeout):
    response = page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
    if response is None or not response.ok:
        raise RuntimeError('SOL request failed (%s): %s' %
                           (response.status if response else 'no response', url))
    return response


def collect_result_urls(page, query, timeout):
    """Validate every result page against SOL's reported item range/count."""
    urls, seen, expected, number = [], set(), None, 1
    while True:
        visit(page, build_search_url(query, number), timeout)
        links = page.locator('a.record_title').evaluate_all('(els) => els.map(e => e.href)')
        count = page.locator('.articles_count').all_text_contents()
        match = re.search(r'(\d+)\s+a\s+(\d+)\s+de\s+(\d+)\s+itens', ' '.join(count))
        if not match:
            body = page.locator('body').inner_text().casefold()
            if number == 1 and not links and ('nenhum resultado encontrado' in body or 'no results found' in body):
                return []
            raise RuntimeError('Unrecognized SOL results page; cannot verify completeness.')
        start, end, total = map(int, match.groups())
        if expected is None:
            expected = total
        if total != expected or start != len(urls) + 1 or end - start + 1 != len(links):
            raise RuntimeError('SOL result counts changed or pagination is incomplete; rerun the search.')
        for url in links:
            if url in seen:
                raise RuntimeError('SOL repeated a result across pages: %s' % url)
            seen.add(url)
            urls.append(url)
        if end == expected:
            return urls
        if not links or end > expected:
            raise RuntimeError('Invalid SOL result range.')
        number += 1


class CitationText(HTMLParser):
    """Extract the BibTeX textarea supplied by SOL's citation plugin."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'textarea':
            self.inside = True

    def handle_endtag(self, tag):
        if tag == 'textarea':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def metadata_entry(metadata, url, abstract='', keywords=''):
    """Convert public citation/DC metadata to a fallback record and enrichment."""
    values = {}
    for name, value in metadata:
        if value and value.strip():
            values.setdefault(name.casefold(), []).append(' '.join(value.split()))
    def first(*names):
        return next((values[name][0] for name in names if values.get(name)), '')
    journal = first('citation_journal_title')
    conference = first('citation_conference_title', 'citation_conference')
    entry = {'ENTRYTYPE': 'article' if journal else 'inproceedings' if conference else 'misc',
             'ID': 'sol_' + hashlib.sha256(url.encode()).hexdigest()[:16], 'url': url}
    fields = {
        'title': first('citation_title', 'dc.title'),
        'author': ' and '.join(values.get('citation_author', [])),
        'doi': first('citation_doi', 'dc.identifier.doi'),
        'journal': journal, 'booktitle': conference,
        'year': first('citation_publication_date', 'citation_date', 'dc.date.issued')[:4],
        'publisher': first('citation_publisher'), 'volume': first('citation_volume'),
        'number': first('citation_issue'), 'issn': first('citation_issn'),
        'abstract': first('dc.description', 'description') or abstract,
        'keywords': keywords or '; '.join(values.get('citation_keywords', []) + values.get('dc.subject', [])),
    }
    for name, value in fields.items():
        if value:
            entry[name] = escape_bibtex(value)
    return entry


def extract_entry(page, url, timeout, logger):
    visit(page, url, timeout)
    metadata = page.locator('meta[name][content]').evaluate_all(
        '(els) => els.map(e => [e.name, e.content])')
    abstract = page.locator('.abstract').first
    abstract_text = abstract.inner_text() if abstract.count() else ''
    abstract_text = re.sub(r'^\s*(Resumo|Abstract)\s*:?\s*', '', abstract_text)
    keywords = page.locator('.keywords .value').all_text_contents()
    fallback = metadata_entry(metadata, url, abstract_text, ' '.join(' '.join(keywords).split()))
    citation = page.locator(CITATION_SELECTOR).first
    entry = None
    if citation.count():
        citation_url = citation.get_attribute('href')
        citation_url = urljoin(url, citation_url)
        if 'BibtexCitationPlugin' in citation_url:
            citation_url = citation_url.rstrip('/') + '/json'
        try:
            response = visit(page, citation_url, timeout)
            text = response.text()
            if text.lstrip().startswith('{'):
                text = json.loads(text)['content']
            if not text.lstrip().startswith('@'):
                parser = CitationText()
                parser.feed(text)
                text = ''.join(parser.parts)
            records = parse_bibtex(text, source=citation_url, require_titles=True).entries
            if len(records) != 1:
                raise ValueError('Expected exactly one SOL citation.')
            entry = records[0]
        except Exception as error:
            logger.warning('Citation export failed for %s; using page metadata: %s', url, error)
    else:
        logger.warning('No BibTeX export for %s; using page metadata.', url)
    if entry is None:
        entry = dict(fallback)
    for field, value in fallback.items():
        if not entry.get(field, '').strip():
            entry[field] = value
    if not entry.get('title', '').strip():
        raise ValueError('SOL paper has no title: %s' % url)
    entry['ID'] = fallback['ID']  # SOL exports reuse keys such as "sbr".
    entry['source'] = merge_sources(entry.get('source', ''), 'SOL')
    return entry


def search_to_bibtex(query, output, *, timeout=60, headless=True, logger=None):
    """Collect all pages and citations in a fresh browser, then atomically save."""
    build_search_url(query)
    if timeout <= 0:
        raise ValueError('Timeout must be positive.')
    logger = logger or logging.getLogger(__name__)
    from playwright.sync_api import sync_playwright
    import tempfile
    with tempfile.TemporaryDirectory(prefix='sol-search-') as profile, sync_playwright() as runtime:
        context = launch_context(runtime, profile, headless=headless)
        try:
            page = context.new_page()
            urls = collect_result_urls(page, query, timeout)
            logger.info('SOL returned %d results.', len(urls))
            entries = [extract_entry(page, url, timeout, logger) for url in urls]
        finally:
            context.close()
    entries = merge_entries([entries], logger)
    write_bibtex(new_database(entries), output)
    return len(entries)


def same_file(left, right):
    return left.resolve() == right.resolve() or (left.exists() and right.exists() and left.samefile(right))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--query', help='Canonical Boolean search expression.')
    group.add_argument('--query-file', type=Path, help='UTF-8 file containing the expression.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--log-file', type=Path, default=Path('sol_search.log'))
    parser.add_argument('--timeout', type=float, default=60, help='Navigation timeout in seconds.')
    parser.add_argument('--show-browser', action='store_true')
    args = parser.parse_args(argv)
    logger = logging.Logger('sol_search', level=logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    if any(same_file(args.log_file, path) for path in (args.output, args.query_file) if path is not None):
        logger.error('Log file must differ from input and output files.')
        return 1
    try:
        handler = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open SOL log')
        return 1
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        if args.query_file and same_file(args.query_file, args.output):
            raise ValueError('Output must differ from the query file.')
        query = args.query_file.read_text(encoding='utf-8-sig') if args.query_file else args.query
        count = search_to_bibtex(query.strip(), args.output, timeout=args.timeout,
                                 headless=not args.show_browser, logger=logger)
        print('Saved %d records to %s' % (count, args.output))
        return 0
    except Exception:
        logger.exception('SOL search/export failed')
        return 1
    finally:
        handler.close()


if __name__ == '__main__':
    sys.exit(main())
