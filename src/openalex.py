"""Shared OpenAlex client and BibTeX metadata conversion."""

import os
import re
from urllib.parse import quote

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bibtex_io import escape_bibtex
from citation_metadata import doi_of, normalized

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

    def work(self, identifier):
        identifier = work_id({'id': identifier})
        if not identifier:
            return None
        return self.get(WORKS + '/' + identifier, missing_ok=True)

    def find(self, entry):
        doi = doi_of(entry)
        if doi:
            work = self.get(WORKS + '/https://doi.org/' + quote(doi, safe='/'), missing_ok=True)
            return work if work and work_id(work) and work_doi(work) == doi else None
        identifier = work_id({'id': entry.get('openalex_id')})
        if identifier:
            return self.work(identifier)

        title = entry.get('title', '')
        year = entry.get('year', '')
        family = author_family(entry.get('author', '').split(' and ')[0])
        if not normalized(title) or not (family or year):
            return None
        matches = {}
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
                matches[identifier] = candidate
        return next(iter(matches.values())) if len(matches) == 1 else None

    def resolve(self, entry):
        return work_id(self.find(entry) or {})

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


def configure_session(session):
    session.headers['User-Agent'] = 'systematic-review-tools'
    key = os.environ.get('OPENALEX_API_KEY', '').strip()
    if key:
        session.headers['Authorization'] = 'Bearer ' + key
    session.mount('https://api.openalex.org/', HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))))
