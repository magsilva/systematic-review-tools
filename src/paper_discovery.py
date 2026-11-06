"""Title-based Crossref discovery and conservative verification of downloaded PDFs."""

from difflib import SequenceMatcher
import html
import re
import unicodedata
from urllib.parse import quote, urlencode, urlsplit


class PdfVerificationError(ValueError):
    pass


def normalize(text):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    text = re.sub(r"\\[\"'`^~=.uvHckbdtr]\s*\{?([A-Za-z])\}?", r'\1', text)
    text = re.sub(r'\\[A-Za-z]+', ' ', text)
    text = unicodedata.normalize('NFKD', text).casefold()
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in text if not unicodedata.combining(c))))


def author_surnames(entry):
    names = []
    for name in re.split(r'\s+and\s+', entry.get('author') or entry.get('editor', ''), flags=re.I):
        name = name.strip()
        if not name or name.casefold() == 'others':
            continue
        family = name.split(',')[0] if ',' in name else name.split()[-1]
        normalized = normalize(family)
        if normalized:
            names.append(normalized)
    return list(dict.fromkeys(names))


def discovery_url(entry):
    return 'https://api.crossref.org/works?' + urlencode({'query.title': entry['title'], 'rows': 5})


def rank_candidates(entry, payload):
    """Reject weak title matches and conflicting author/year metadata before download."""
    title = normalize(entry.get('title', ''))
    expected_authors = author_surnames(entry)
    candidates = []
    for item in payload.get('message', {}).get('items', []):
        titles = item.get('title') or []
        candidate_title = titles[0] if titles else ''
        match = normalize(candidate_title)
        score = SequenceMatcher(None, title, match).ratio() if title and match else 0
        if score < 0.92:
            continue
        authors = [normalize(a.get('family', '')) for a in (item.get('author') or item.get('editor') or [])]
        authors = [a for a in authors if a]
        if expected_authors and authors:
            overlap = len(set(expected_authors).intersection(authors)) / len(set(expected_authors))
            if overlap < 0.75:
                continue
        date = item.get('published', {}).get('date-parts', [[]])
        year = str(date[0][0]) if date and date[0] else ''
        if entry.get('year') and year and entry['year'].strip('{} ') != year:
            continue
        doi = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '',
                     item.get('DOI', '').strip(), flags=re.I)
        if doi and not re.fullmatch(r'10\.\d{4,9}/\S+', doi, flags=re.I):
            continue
        urls = [link.get('URL') for link in item.get('link', [])
                if link.get('content-type', '').casefold() == 'application/pdf']
        urls.extend([item.get('resource', {}).get('primary', {}).get('URL'), item.get('URL')])
        if doi:
            urls.append('https://doi.org/' + quote(doi, safe='/():;._-'))
        urls = list(dict.fromkeys(url for url in urls if isinstance(url, str)
                                 and urlsplit(url).scheme in ('http', 'https') and urlsplit(url).hostname))
        abstract = entry.get('abstract') or item.get('abstract', '')
        validation_authors = expected_authors or authors
        if not urls or not validation_authors or len(normalize(abstract).split()) < 8:
            continue
        candidates.append({'title': candidate_title, 'doi': doi, 'urls': urls, 'score': score,
                           'authors': validation_authors, 'abstract': abstract})
    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates


def extract_pdf_text(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(''):
        raise PdfVerificationError('PDF is encrypted; cannot verify its contents.')
    text = []
    for page in reader.pages[:3]:
        text.append(page.extract_text() or '')
    return '\n'.join(text)


def verify_pdf(path, entry, candidate):
    """Require title, author surnames, and abstract evidence in the PDF itself."""
    try:
        text = normalize(extract_pdf_text(path))
    except PdfVerificationError:
        raise
    except Exception as error:
        raise PdfVerificationError('Could not extract PDF text: %s' % error) from error
    if not text:
        raise PdfVerificationError('PDF has no extractable text; verification requires text/OCR.')
    front = ' '.join(text.split()[:250])
    title = normalize(entry['title'])
    if title not in front:
        title_words = title.split()
        windows = [' '.join(front.split()[n:n+len(title_words)])
                   for n in range(max(1, len(front.split())-len(title_words)+1))]
        if max(SequenceMatcher(None, title, window).ratio() for window in windows) < 0.9:
            raise PdfVerificationError('PDF title does not match the BibTeX title.')
    authors = candidate['authors']
    header = ' ' + ' '.join(text.split()[:500]) + ' '
    if not authors or sum((' ' + name + ' ') in header for name in authors) / len(authors) < 0.75:
        raise PdfVerificationError('PDF authors do not match the expected authors.')
    words = normalize(candidate['abstract']).split()
    pairs = set(zip(words, words[1:]))
    if len(words) < 8 or not pairs:
        raise PdfVerificationError('Insufficient abstract information to verify PDF.')
    pdf_pairs = set(zip(text.split(), text.split()[1:]))
    if len(pairs & pdf_pairs) / len(pairs) < 0.65:
        raise PdfVerificationError('PDF abstract does not match the expected abstract.')
