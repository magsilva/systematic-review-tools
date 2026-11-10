"""Project-wide python-bibtexparser reading, serialization, and atomic writing."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tempfile


def escape_bibtex(value):
    """Escape literal metadata for a braced BibTeX field, preserving Unicode."""
    replacements = {
        '\\': r'\textbackslash{}', '{': r'\textbraceleft{}', '}': r'\textbraceright{}',
        '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
        '_': r'\_', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
    }
    return ''.join(replacements.get(char, char) for char in str(value))


def balance_literal_braces(content):
    """Represent escaped literal braces with balanced TeX commands before parsing."""
    def replace(match):
        slashes, brace = match.groups()
        if len(slashes) % 2 == 0:
            return match.group()
        command = 'textbraceleft' if brace == '{' else 'textbraceright'
        return slashes[:-1] + '\\' + command + '{}'
    return re.sub(r'(\\+)([{}])', replace, content)


@dataclass
class BibtexRecords:
    """Mutable application records, independent of the parser's object model."""
    entries: list = field(default_factory=list)
    preambles: list = field(default_factory=list)


def new_database(entries=(), preambles=()):
    return BibtexRecords([dict(entry) for entry in entries], list(preambles))


def _library(database):
    from bibtexparser import Library
    from bibtexparser.model import Entry, Field, Preamble
    blocks = [Preamble(value) for value in database.preambles]
    blocks.extend(Entry(entry['ENTRYTYPE'], entry['ID'],
                        [Field(key, value) for key, value in entry.items()
                         if key not in ('ENTRYTYPE', 'ID')])
                  for entry in database.entries)
    return Library(blocks)


def _format(indent):
    from bibtexparser.writer import BibtexFormat
    formatting = BibtexFormat()
    formatting.indent = indent
    return formatting


def serialize_bibtex(database, *, indent='  '):
    import bibtexparser
    return bibtexparser.write_string(_library(database), bibtex_format=_format(indent))


def write_bibtex(database, output, *, indent='  '):
    """Serialize with bibtexparser 2, replacing output only after success."""
    import bibtexparser
    output = Path(output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            bibtexparser.write_file(stream, _library(database), bibtex_format=_format(indent))
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def read_bibtex(path, *, encoding='utf-8-sig', fallback_encoding=None, **options):
    path = Path(path)
    try:
        content = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        if fallback_encoding is None:
            raise
        content = path.read_text(encoding=fallback_encoding)
    return parse_bibtex(content, path, **options)


def merge_sources(*values):
    """Return a deterministic, case-insensitive union of semicolon-separated sources."""
    names = {}
    canonical = {'acm': 'ACM', 'scopus': 'Scopus'}
    for value in values:
        for name in value.split(';'):
            name = name.strip()
            if name:
                key = name.casefold()
                names.setdefault(key, canonical.get(key, name))
    return '; '.join(names[key] for key in sorted(names))


class BibtexParseError(ValueError):
    """A parse/validation failure associated with a one-based source line."""

    def __init__(self, source, line, message):
        self.source = str(source)
        self.line = line
        super().__init__('%s: line %d: %s' % (source, line, message))


def parse_bibtex(content, source='<string>', *, require_titles=False, plaintext=False,
                 inherit_crossref=False):
    """Parse with v2 middleware and reject skipped blocks with source lines."""
    import bibtexparser
    from bibtexparser.middlewares import NormalizeFieldKeys, default_parse_stack
    from bibtexparser.middlewares import LibraryMiddleware

    class ValidateReferences(LibraryMiddleware):
        def transform(self, library):
            strings = {key.casefold() for key in library.strings_dict}
            months = dict(zip(
                ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'),
                ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
                 'September', 'October', 'November', 'December')))
            for entry in library.entries:
                for field in entry.fields:
                    value = field.value
                    if not isinstance(value, str) or value.startswith(('{', '"')) or value.isdecimal():
                        continue
                    if value.casefold() in strings:
                        continue
                    if value.casefold() in months:
                        field.value = '{' + months[value.casefold()] + '}'
                        continue
                    raise BibtexParseError(source, (entry.start_line or 0) + 1,
                                           'Undefined string macro: ' + value)
            return library

    library = bibtexparser.parse_string(
        balance_literal_braces(content),
        parse_stack=[ValidateReferences(), *default_parse_stack(), NormalizeFieldKeys()])
    rejected = list(library.failed_blocks) + list(library.comments)
    if rejected:
        block = min(rejected, key=lambda block: block.start_line or 0)
        raise BibtexParseError(source, (block.start_line or 0) + 1,
                               'Unparsed content or comments; check the BibTeX record/block starting here.')
    for entry in library.entries:
        if require_titles and not entry.get('title'):
            raise BibtexParseError(source, (entry.start_line or 0) + 1,
                                   'BibTeX entry %s is missing a title.' % entry.key)
    database = new_database(library.entries, [block.value for block in library.preambles])
    for entry in database.entries:
        entry['ENTRYTYPE'] = entry['ENTRYTYPE'].lower()
    if inherit_crossref:
        by_key = {entry['ID']: entry for entry in database.entries}
        def inherit(entry, visiting):
            parent = by_key.get(entry.get('crossref'))
            if parent is None:
                return
            if entry['ID'] in visiting:
                raise BibtexParseError(source, 1, 'Circular crossref reference.')
            inherit(parent, visiting | {entry['ID']})
            for key, value in parent.items():
                if key not in ('ID', 'ENTRYTYPE', 'crossref'):
                    entry.setdefault(key, value)
        for entry in database.entries:
            inherit(entry, set())
    if plaintext:
        from pylatexenc.latex2text import LatexNodes2Text
        decoder = LatexNodes2Text()
        for entry in database.entries:
            entry.update({'plain_' + key: decoder.latex_to_text(value)
                          for key, value in list(entry.items())
                          if key not in ('ID', 'ENTRYTYPE')})
    if require_titles and not database.entries:
        raise BibtexParseError(source, 1, 'Export must contain BibTeX entries with titles.')
    return database
