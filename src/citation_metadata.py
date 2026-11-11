"""Normalize identifiers and text for citation lookups."""

from html import unescape
import re
import unicodedata

from merge_bibtex import normalize_doi


def doi_of(entry):
    doi = normalize_doi(str(entry.get('DOI') or entry.get('doi') or ''))
    url = str(entry.get('url') or '')
    if not doi and re.match(r'https?://(?:dx\.)?doi\.org/', url, re.I):
        doi = normalize_doi(url)
    return doi if re.fullmatch(r'10\.\d{4,}/\S+', doi) else ''


def normalized(value):
    value = unicodedata.normalize('NFKD', unescape(str(value)).casefold())
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in value if not unicodedata.combining(c))))
