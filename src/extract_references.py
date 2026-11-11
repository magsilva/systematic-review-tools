#!/usr/bin/env python3
"""Export direct references for a BibTeX bibliography using Crossref and OpenAlex."""

import argparse
from collections import Counter
from html import unescape
import logging
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote

import requests

from bibtex_interrupt import bibtex_checkpoint, interrupt_exit, track_bibtex
from bibtex_io import escape_bibtex, merge_sources, new_database, read_bibtex, write_bibtex
from citation_metadata import doi_of, normalized
from openalex import OpenAlex, citation_entry, configure_session, work_doi, work_id
from scholar_references import Scholar

WORKS = 'https://api.crossref.org/works'
TYPES = {'journal-article': 'article', 'proceedings-article': 'inproceedings',
         'book-chapter': 'incollection', 'book': 'book', 'monograph': 'book',
         'proceedings': 'proceedings'}


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


def metadata_incomplete(entry):
    fields = ('author', 'title', 'doi', 'year', 'abstract', 'keywords')
    return any(not entry.get(field) for field in fields) or not (
        entry.get('journal') or entry.get('booktitle'))


def extract(entries, client, logger, scholar=None, openalex=None):
    results, stats = {}, Counter()
    track_bibtex(lambda: new_database(results.values()))
    lookup_failed = object()

    def attempt(operation, label, *, on_error=None):
        try:
            return operation()
        except (requests.RequestException, ValueError, KeyError, OSError) as error:
            logger.warning('%s: %s', label, error)
            stats['errors'] += 1
            return on_error

    for parent in entries:
        doi = doi_of(parent)
        source = doi or parent['ID']
        if doi:
            work = attempt(lambda: client.work(doi), source, on_error=lookup_failed)
        else:
            work = attempt(lambda: client.find(title=parent.get('title', ''),
                                               author=parent.get('author', '').split(' and ')[0],
                                               year=parent.get('year', '')), source,
                           on_error=lookup_failed)
            if work is not None and work is not lookup_failed:
                source = doi_of(work) or source
        refs = (work.get('reference') or []) if isinstance(work, dict) else []
        alex_parent = None
        if not refs and openalex is not None:
            identity = dict(parent)
            if isinstance(work, dict) and doi_of(work):
                identity['doi'] = doi_of(work)
            alex_parent = attempt(lambda: openalex.find(identity), source + ' (OpenAlex)',
                                  on_error=lookup_failed)
            if isinstance(alex_parent, dict):
                source = work_doi(alex_parent) or source
                refs = [{'openalex_id': identifier} for identifier in
                        dict.fromkeys(alex_parent.get('referenced_works') or [])]
        if work is None and alex_parent is None and scholar is not None:
            found = attempt(lambda: scholar.find(
                doi=doi, title=parent.get('title', ''),
                author=parent.get('author', '').split(' and ')[0],
                year=parent.get('year', '')), source + ' (Google Scholar)')
            recovered = doi_of(found) if found else ''
            if recovered and recovered != doi:
                work = attempt(lambda: client.work(recovered), recovered)
                source = recovered
                refs = (work.get('reference') or []) if work else []
        if not refs:
            logger.warning('%s: no reference list available', source)
            stats['parents_without_references'] += 1
        for position, ref in enumerate(refs, 1):
            stats['references'] += 1
            cited_doi = doi_of(ref)
            label = '%s reference %d' % (source, position)
            alex_work = None
            if ref.get('openalex_id'):
                alex_work = attempt(lambda: openalex.work(ref['openalex_id']), label + ' (OpenAlex)',
                                    on_error=lookup_failed)
                if isinstance(alex_work, dict):
                    cited_doi = work_doi(alex_work)
            if cited_doi:
                metadata = attempt(lambda: client.work(cited_doi), label, on_error=lookup_failed)
            else:
                metadata = attempt(lambda: client.find(
                    title=ref.get('article-title', ''), author=ref.get('author', ''),
                    year=str(ref.get('year', '')), citation=ref.get('unstructured', '')), label,
                    on_error=lookup_failed)
            scholar_entry = None
            crossref_missed = metadata is None
            if metadata is lookup_failed:
                metadata = None
            entry = reference_fields(ref, metadata)
            if openalex is not None and alex_work is None and not ref.get('openalex_id') and metadata_incomplete(entry):
                identity = {
                    'doi': entry.get('doi', ''),
                    'title': first((metadata or {}).get('title')) or ref.get('article-title') or ref.get('volume-title') or '',
                    'author': entry.get('author', ''),
                    'year': entry.get('year', ''),
                }
                alex_work = attempt(lambda: openalex.find(identity), label + ' (OpenAlex)',
                                    on_error=lookup_failed)
            if isinstance(alex_work, dict):
                extra = citation_entry(alex_work)
                if entry['ENTRYTYPE'] == 'misc':
                    entry['ENTRYTYPE'] = extra['ENTRYTYPE']
                # Keep the provider's venue only when Crossref supplied none.
                if entry.get('journal') or entry.get('booktitle'):
                    extra.pop('journal', None)
                    extra.pop('booktitle', None)
                for field, value in extra.items():
                    entry.setdefault(field, value)
                stats['openalex_matches'] += 1
            if crossref_missed and alex_work is None and scholar is not None:
                scholar_entry = attempt(lambda: scholar.find(
                    doi=cited_doi, title=ref.get('article-title', ''),
                    author=ref.get('author', ''), year=str(ref.get('year', '')),
                    citation=ref.get('unstructured', '')), label + ' (Google Scholar)')
                if scholar_entry:
                    recovered = doi_of(scholar_entry) or cited_doi
                    if recovered and recovered != cited_doi:
                        metadata = attempt(lambda: client.work(recovered), label)
                        if metadata:
                            entry = reference_fields(ref, metadata)
                    for field in ('author', 'title', 'journal', 'booktitle', 'year',
                                  'abstract', 'keywords', 'publisher', 'url'):
                        if scholar_entry.get(field) and (not metadata or field not in entry):
                            entry[field] = scholar_entry[field]
                    if recovered:
                        entry['doi'] = recovered
                    if entry['ENTRYTYPE'] == 'misc':
                        entry['ENTRYTYPE'] = scholar_entry['ENTRYTYPE']
                    stats['scholar_matches'] += 1
            if metadata is None and not isinstance(alex_work, dict) and not scholar_entry:
                stats['unresolved'] += 1
                logger.warning('%s: no verified metadata match; retaining deposited citation', label)
                if len(entry) == 1:
                    entry['note'] = 'Crossref reference %d; no bibliographic details deposited' % position
            # Without a DOI only combine records with identical deposited details.
            if entry.get('doi'):
                key = ('doi', entry['doi'])
            elif entry.get('openalex_id'):
                key = ('openalex', entry['openalex_id'])
            elif len(entry) > 1 and any(ref.get(k) for k in ('unstructured', 'article-title', 'volume-title')):
                key = ('citation', tuple(sorted(entry.items())))
            else:
                key = ('position', source, position)
            stored = dict(results[key]) if key in results else dict(
                entry, ID='reference_%d' % (len(results) + 1))
            for field, value in entry.items():
                if field == 'ENTRYTYPE' and stored[field] == 'misc':
                    stored[field] = value
                else:
                    stored.setdefault(field, value)
            stored['source'] = merge_sources(stored.get('source', ''),
                                              (scholar_entry or {}).get('source', ''), source)
            results[key] = stored
    return list(results.values()), stats


@interrupt_exit
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('--mailto', help='Optional contact email for Crossref’s polite pool.')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--interval', type=float, default=0.5, help='Minimum seconds between requests.')
    parser.add_argument('--log-file', type=Path, default=Path('extract_references.log'))
    parser.add_argument('--scholar-bibtex', type=Path,
                        help='Use an existing Google Scholar BibTeX export as a fallback.')
    parser.add_argument('--no-scholar', action='store_true', help='Disable Google Scholar fallback.')
    parser.add_argument('--no-openalex', action='store_true', help='Disable OpenAlex fallback.')
    parser.add_argument('--scholar-manual', action='store_true',
                        help='Check supplied Scholar exports and log unresolved lookup links.')
    args = parser.parse_args(argv)
    if args.no_scholar and args.scholar_bibtex:
        parser.error('--no-scholar and --scholar-bibtex cannot be combined.')
    if not all(math.isfinite(v) and v > 0 for v in (args.timeout, args.interval)):
        parser.error('--timeout and --interval must be finite positive numbers.')
    paths = (args.input, args.output, args.log_file)
    if args.scholar_bibtex:
        paths += (args.scholar_bibtex,)
    for index, a in enumerate(paths):
        for b in paths[index + 1:]:
            if a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b)):
                parser.error('Input, output and log must be different files.')
    logger = logging.Logger('extract_references')
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(sys.stderr))
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Cannot open reference extraction log')
        return 1
    scholar = None
    try:
        logger.addHandler(log)
        with bibtex_checkpoint(args.output):
            entries = read_bibtex(args.input).entries
            scholar = None if args.no_scholar else Scholar(
                entries=read_bibtex(args.scholar_bibtex).entries if args.scholar_bibtex else [],
                logger=logger, identify=doi_of, normalize=normalized,
                online=not args.scholar_manual, timeout=args.timeout)
            with requests.Session() as session, requests.Session() as alex_session:
                session.headers['User-Agent'] = 'systematic-review-tools/reference-extractor'
                alex = None
                if not args.no_openalex:
                    configure_session(alex_session)
                    alex = OpenAlex(alex_session, timeout=args.timeout)
                results, stats = extract(entries, Crossref(session, mailto=args.mailto,
                                                          timeout=args.timeout, interval=args.interval),
                                         logger, scholar, alex)
            if entries and not results and stats['errors']:
                raise RuntimeError('No references retrieved; preserving any existing output.')
            write_bibtex(new_database(results), args.output)
            print('Read %d papers; exported %d unique references from %d citations. '
                  '%d papers without reference lists; %d OpenAlex matches; %d Scholar matches; '
                  '%d unresolved citations; %d errors. BibTeX: %s'
                  % (len(entries), len(results), stats['references'], stats['parents_without_references'],
                     stats['openalex_matches'], stats['scholar_matches'], stats['unresolved'],
                     stats['errors'], args.output))
            return 1 if stats['errors'] else 0
    except Exception:
        logger.exception('Reference extraction failed')
        return 1
    finally:
        try:
            if scholar is not None:
                scholar.close()
        finally:
            log.close()


if __name__ == '__main__':
    sys.exit(main())
