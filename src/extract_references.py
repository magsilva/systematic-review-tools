#!/usr/bin/env python3
"""Export the direct references deposited in Crossref for a BibTeX bibliography."""

import argparse
from collections import Counter
from html import unescape
import logging
import math
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.parse import quote

import requests

from bibtex_io import escape_bibtex, merge_sources, new_database, read_bibtex, write_bibtex
from merge_bibtex import normalize_doi

WORKS = 'https://api.crossref.org/works'
TYPES = {'journal-article': 'article', 'proceedings-article': 'inproceedings',
         'book-chapter': 'incollection', 'book': 'book', 'monograph': 'book',
         'proceedings': 'proceedings'}


def doi_of(entry):
    doi = normalize_doi(str(entry.get('DOI') or entry.get('doi') or ''))
    url = str(entry.get('url') or '')
    if not doi and re.match(r'https?://(?:dx\.)?doi\.org/', url, re.I):
        doi = normalize_doi(url)
    return doi if re.fullmatch(r'10\.\d{4,}/\S+', doi) else ''


def normalized(value):
    value = unicodedata.normalize('NFKD', unescape(str(value)).casefold())
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in value if not unicodedata.combining(c))))


def first(values):
    return values[0] if isinstance(values, list) and values else ''


def year_of(work):
    for field in ('issued', 'published', 'published-print', 'published-online'):
        parts = first(work.get(field, {}).get('date-parts', []))
        if parts:
            return str(parts[0])
    return ''


class Crossref:
    """Sequential requests with a run-local cache and bounded transient retries."""

    def __init__(self, session, *, mailto=None, timeout=30, interval=0.5):
        self.session = session
        self.params = {'mailto': mailto} if mailto else {}
        self.timeout = timeout
        self.interval = interval
        self.next_request = 0
        self.cache = {}

    def get(self, url, **params):
        key = url, tuple(sorted(params.items()))
        if key in self.cache:
            return self.cache[key]
        for attempt in range(3):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval
            try:
                response = self.session.get(url, params={**self.params, **params}, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
            else:
                if response.status_code == 404:
                    self.cache[key] = None
                    return None
                if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                    response.raise_for_status()
                    result = response.json()['message']
                    self.cache[key] = result
                    return result
                retry = response.headers.get('Retry-After', '')
                if retry.isdecimal():
                    self.next_request = max(self.next_request, time.monotonic() + int(retry))
            self.next_request = max(self.next_request, time.monotonic() + 2 ** attempt)

    def work(self, doi):
        return self.get(WORKS + '/' + quote(doi, safe='/'))

    def find(self, *, title='', author='', year='', citation=''):
        """Accept a unique corroborated match, never just the top search hit."""
        if not (title or citation):
            return None
        query = citation or ' '.join(filter(None, (title, author, year)))
        result = self.get(WORKS, rows=5, **{'query.bibliographic': query}) or {}
        matches = {}
        for item in result.get('items', []):
            doi = doi_of(item)
            candidate = normalized(first(item.get('title')))
            family = normalized((first(item.get('author')) or {}).get('family', ''))
            item_year = year_of(item)
            if not doi or not candidate or (year and year != item_year):
                continue
            if title and candidate != normalized(title):
                continue
            haystack = ' ' + normalized(citation or author) + ' '
            author_matches = bool(family and ' ' + family + ' ' in haystack)
            if author and not author_matches:
                continue
            if not title and not (len(candidate.split()) >= 3 and ' ' + candidate + ' ' in haystack
                                  and author_matches and item_year
                                  and ' ' + item_year + ' ' in haystack):
                continue
            if title and not (author_matches or year):
                continue
            matches[doi] = item
        return next(iter(matches.values())) if len(matches) == 1 else None


def reference_fields(ref, work):
    work = work or {}
    venue = first(work.get('container-title')) or ref.get('journal-title') or ref.get('volume-title')
    kind = TYPES.get(work.get('type'), 'article' if ref.get('journal-title') else 'misc')
    names = ['{}, {}'.format(a['family'], a['given']) if a.get('family') and a.get('given')
             else a.get('family') or a.get('name') or a.get('given') or ''
             for a in work.get('author', [])]
    abstract = unescape(re.sub(r'<[^>]+>', ' ', work.get('abstract') or ''))
    keywords = work.get('keywords') or work.get('subject') or []
    if isinstance(keywords, str):
        keywords = [keywords]
    values = {
        'author': ' and '.join(filter(None, names)) or ref.get('author'),
        'title': first(work.get('title')) or ref.get('article-title') or ref.get('volume-title'),
        'booktitle' if kind in ('inproceedings', 'incollection') else 'journal': venue,
        'year': year_of(work) or ref.get('year'),
        'doi': doi_of(work) or doi_of(ref),
        'abstract': ' '.join(abstract.split()),
        'keywords': ', '.join(keywords),
        'publisher': work.get('publisher'),
        'url': work.get('URL'),
        'note': ref.get('unstructured') if not work else None,
    }
    entry = {key: str(value).strip() if key in ('doi', 'url') else escape_bibtex(str(value).strip()) for key, value in values.items()
             if value is not None and str(value).strip()}
    return dict(entry, ENTRYTYPE=kind)


def extract(entries, client, logger):
    results, stats = {}, Counter()

    def attempt(operation, label):
        try:
            return operation()
        except (requests.RequestException, ValueError, KeyError) as error:
            logger.warning('%s: %s', label, error)
            stats['errors'] += 1
            return None

    for parent in entries:
        doi = doi_of(parent)
        source = doi or parent['ID']
        if doi:
            work = attempt(lambda: client.work(doi), source)
        else:
            work = attempt(lambda: client.find(title=parent.get('title', ''),
                                               author=parent.get('author', '').split(' and ')[0],
                                               year=parent.get('year', '')), source)
            if work:
                source = doi_of(work) or source
        refs = work.get('reference', []) if work else []
        if not refs:
            logger.warning('%s: no reference list available from Crossref', source)
            stats['parents_without_references'] += 1
        for position, ref in enumerate(refs, 1):
            stats['references'] += 1
            cited_doi = doi_of(ref)
            label = '%s reference %d' % (source, position)
            if cited_doi:
                metadata = attempt(lambda: client.work(cited_doi), label)
            else:
                metadata = attempt(lambda: client.find(
                    title=ref.get('article-title', ''), author=ref.get('author', ''),
                    year=str(ref.get('year', '')), citation=ref.get('unstructured', '')), label)
            entry = reference_fields(ref, metadata)
            if not metadata:
                stats['unresolved'] += 1
                logger.warning('%s: no verified metadata match; retaining deposited citation', label)
                if len(entry) == 1:
                    entry['note'] = 'Crossref reference %d; no bibliographic details deposited' % position
            # Without a DOI only combine records with identical deposited details.
            if entry.get('doi'):
                key = ('doi', entry['doi'])
            elif len(entry) > 1 and any(ref.get(k) for k in ('unstructured', 'article-title', 'volume-title')):
                key = ('citation', tuple(sorted(entry.items())))
            else:
                key = ('position', source, position)
            if key not in results:
                results[key] = dict(entry, ID='reference_%d' % (len(results) + 1))
            stored = results[key]
            for field, value in entry.items():
                if field == 'ENTRYTYPE' and stored[field] == 'misc':
                    stored[field] = value
                else:
                    stored.setdefault(field, value)
            stored['source'] = merge_sources(stored.get('source', ''), source)
    return list(results.values()), stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--mailto', help='Optional contact email for Crossref’s polite pool.')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--interval', type=float, default=0.5, help='Minimum seconds between requests.')
    parser.add_argument('--log-file', type=Path, default=Path('extract_references.log'))
    args = parser.parse_args(argv)
    if not all(math.isfinite(v) and v > 0 for v in (args.timeout, args.interval)):
        parser.error('--timeout and --interval must be finite positive numbers.')
    paths = (args.input, args.output, args.log_file)
    for index, a in enumerate(paths):
        for b in paths[index + 1:]:
            if a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b)):
                parser.error('Input, output and log must be different files.')
    logger = logging.Logger('extract_references')
    logger.addHandler(logging.StreamHandler(sys.stderr))
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Cannot open reference extraction log')
        return 1
    try:
        logger.addHandler(log)
        entries = read_bibtex(args.input).entries
        with requests.Session() as session:
            session.headers['User-Agent'] = 'systematic-review-tools/reference-extractor'
            results, stats = extract(entries, Crossref(session, mailto=args.mailto,
                                                      timeout=args.timeout, interval=args.interval), logger)
        if entries and not results and stats['errors']:
            raise RuntimeError('No references retrieved; preserving any existing output.')
        write_bibtex(new_database(results), args.output)
        print('Read %d papers; exported %d unique references from %d citations. '
              '%d papers without reference lists; %d unresolved citations; %d request errors. BibTeX: %s'
              % (len(entries), len(results), stats['references'], stats['parents_without_references'],
                 stats['unresolved'], stats['errors'], args.output))
        return 1 if stats['errors'] else 0
    except Exception:
        logger.exception('Reference extraction failed')
        return 1
    finally:
        log.close()


if __name__ == '__main__':
    sys.exit(main())
