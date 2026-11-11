#!/usr/bin/env python3
"""Export papers that cite the works in a BibTeX file using OpenAlex."""

import argparse
from collections import Counter
import logging
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bibtex_io import escape_bibtex, merge_sources, new_database, read_bibtex, write_bibtex
from extract_references import doi_of, normalized

WORKS = 'https://api.openalex.org/works'
TYPES = {'article': 'article', 'proceedings-article': 'inproceedings',
         'book-chapter': 'incollection', 'book': 'book', 'dissertation': 'phdthesis',
         'report': 'techreport'}


def work_id(work):
    value = (work.get('id') or '').removeprefix('https://openalex.org/')
    return value if re.fullmatch(r'W[0-9]+', value) else ''


def work_doi(work):
    return doi_of({'doi': work.get('doi')})


def author_names(work):
    return [name for authorship in (work.get('authorships') or [])
            if (name := (authorship.get('author') or {}).get('display_name')
                or authorship.get('raw_author_name'))]


def author_family(name):
    # BibTeX accepts both "Family, Given" and "Given Family".
    parts = normalized(name.split(',', 1)[0]).split()
    return parts[-1] if parts else ''


class OpenAlex:
    def __init__(self, session, *, timeout=30):
        self.session = session
        self.timeout = timeout

    def get(self, url=WORKS, *, missing_ok=False, **params):
        with self.session.get(url, params=params, timeout=self.timeout) as response:
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    def resolve(self, entry):
        doi = doi_of(entry)
        if doi:
            work = self.get(WORKS + '/https://doi.org/' + quote(doi, safe='/'), missing_ok=True)
            return work_id(work) if work and work_doi(work) == doi else ''
        identifier = work_id({'id': entry.get('openalex_id')})
        if identifier:
            work = self.get(WORKS + '/' + identifier, missing_ok=True)
            return work_id(work) if work else ''

        title = entry.get('title', '')
        year = entry.get('year', '')
        family = author_family(entry.get('author', '').split(' and ')[0])
        if not normalized(title) or not (family or year):
            return ''
        matches = set()
        for candidate in self.get(search=title, per_page=10)['results']:
            if normalized(candidate.get('display_name') or '') != normalized(title):
                continue
            if year and str(candidate.get('publication_year') or '') != year:
                continue
            names = author_names(candidate)
            if family and (not names or author_family(names[0]) != family):
                continue
            identifier = work_id(candidate)
            if identifier:
                matches.add(identifier)
        return next(iter(matches)) if len(matches) == 1 else ''

    def citing(self, identifier):
        cursor = '*'
        seen = {cursor}
        retrieved = 0
        while cursor:
            page = self.get(filter='cites:' + identifier, per_page=200, cursor=cursor)
            works = page['results']
            yield from works
            retrieved += len(works)
            meta = page['meta']
            if retrieved >= meta['count']:
                return
            cursor = meta.get('next_cursor')
            if not works or not cursor or cursor in seen:
                raise ValueError('OpenAlex pagination ended before all reported results were retrieved.')
            seen.add(cursor)


def citation_entry(work):
    location = work.get('primary_location') or {}
    venue = location.get('source') or {}
    kind = TYPES.get(work.get('type'), 'misc')
    if kind == 'article' and venue.get('type') == 'conference':
        kind = 'inproceedings'
    indexed_abstract = work.get('abstract_inverted_index') or {}
    words = sorted((position, word) for word, positions in indexed_abstract.items()
                   for position in positions)
    keywords = [keyword['display_name'] for keyword in (work.get('keywords') or [])
                if keyword.get('display_name')]
    doi = work_doi(work)
    values = {
        'title': work.get('display_name') or work.get('title'),
        'author': ' and '.join(author_names(work)),
        'booktitle' if kind in ('inproceedings', 'incollection') else 'journal': venue.get('display_name'),
        'year': work.get('publication_year'),
        'abstract': ' '.join(word for _, word in words),
        'keywords': ', '.join(dict.fromkeys(keywords)),
    }
    entry = {field: escape_bibtex(str(value)) for field, value in values.items() if value}
    entry.update(ENTRYTYPE=kind)
    if doi:
        entry.update(doi=doi, url='https://doi.org/' + doi)
    if work_id(work):
        entry['openalex_id'] = work_id(work)
        entry.setdefault('url', 'https://openalex.org/' + work_id(work))
    return entry


def extract(entries, client, logger):
    records, stats = {}, Counter()
    for parent in entries:
        source = doi_of(parent) or parent['ID']
        try:
            identifier = client.resolve(parent)
            if not identifier:
                logger.warning('%s: no verified OpenAlex match', source)
                stats['unmatched'] += 1
                continue
            for work in client.citing(identifier):
                entry = citation_entry(work)
                key = ('doi', entry['doi']) if 'doi' in entry else ('openalex', work_id(work))
                if not key[1]:
                    logger.warning('%s: skipped citing record with no DOI or OpenAlex ID', source)
                    stats['errors'] += 1
                    continue
                stored = records.setdefault(key, dict(entry, ID='citing_%d' % (len(records) + 1)))
                for field, value in entry.items():
                    stored.setdefault(field, value)
                stored['source'] = merge_sources(stored.get('source', ''), source)
            stats['completed'] += 1
        except (requests.RequestException, ValueError, KeyError) as error:
            logger.warning('%s: %s', source, error)
            stats['errors'] += 1
    return list(records.values()), stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('-o', '--output', type=Path, required=True)
    parser.add_argument('--log-file', type=Path, default=Path('extract_citing_papers.log'))
    args = parser.parse_args(argv)
    paths = (args.input, args.output, args.log_file)
    for index, path in enumerate(paths):
        if any(path.resolve() == other.resolve()
               or (path.exists() and other.exists() and path.samefile(other))
               for other in paths[index + 1:]):
            parser.error('Input, output and log must be different files.')
    logger = logging.Logger('extract_citing_papers')
    logger.addHandler(logging.StreamHandler(sys.stderr))
    log = None
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
        logger.addHandler(log)
        entries = read_bibtex(args.input).entries
        with requests.Session() as session:
            session.headers['User-Agent'] = 'systematic-review-tools/forward-citations'
            key = os.environ.get('OPENALEX_API_KEY', '').strip()
            if key:
                session.headers['Authorization'] = 'Bearer ' + key
            session.mount('https://api.openalex.org/', HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))))
            records, stats = extract(entries, OpenAlex(session), logger)
        if stats['errors'] and not records and not stats['completed']:
            raise RuntimeError('No completed lookups or citing records. Output was preserved.')
        write_bibtex(new_database(records), args.output)
        print('Read %d papers and exported %d citing works to %s. '
              '%d inputs unmatched, %d errors.'
              % (len(entries), len(records), args.output, stats['unmatched'], stats['errors']))
        return 1 if stats['errors'] or stats['unmatched'] else 0
    except Exception:
        logger.exception('Forward citation extraction failed')
        return 1
    finally:
        if log is not None:
            log.close()


if __name__ == '__main__':
    sys.exit(main())
