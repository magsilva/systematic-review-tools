#!/usr/bin/env python3
"""Merge BibTeX files and enrich duplicate records with missing metadata."""

import argparse
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import unquote

from bibtex_io import parse_bibtex


def normalize_doi(value):
    value = unquote(value.strip()).casefold()
    value = re.sub(r'\\([_&%#{}])', r'\1', value)
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value)
    return value.strip().strip('{}').strip()


def normalize_title(value):
    """Ignore case, protective braces, and whitespace, retaining punctuation."""
    value = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(value.replace('{', '').replace('}', '').split())


def balance_literal_braces(content):
    """Use balanced TeX commands for escaped literal braces in field values.

    BibTeX parsers count braces even in \\{ and \\}; an unmatched literal
    brace can otherwise cause an entire record to be silently skipped.
    """
    def replace(match):
        slashes, brace = match.groups()
        if len(slashes) % 2 == 0:
            return match.group()
        command = 'textbraceleft' if brace == '{' else 'textbraceright'
        return slashes[:-1] + '\\' + command + '{}'
    return re.sub(r'(\\+)([{}])', replace, content)


def identity_keys(entry):
    """Prefer identifiers; title/year is a conservative fallback, never ID alone."""
    keys = set()
    doi = normalize_doi(entry.get('doi', ''))
    if not doi:
        url = entry.get('url', '').strip()
        if re.match(r'https?://(?:dx\.)?doi\.org/', url, re.I):
            doi = normalize_doi(url)
    if doi:
        keys.add(('doi', doi))
    eid = entry.get('scopus_eid', '').strip().casefold()
    if eid:
        keys.add(('scopus_eid', eid))
    title = normalize_title(entry.get('title', ''))
    year = entry.get('year', '').strip().strip('{}')
    if title and year:
        keys.add(('title_year', (title, year)))
    return keys


def merge_entries(sources, logger=None):
    """Merge entry sequences in input order; first nonempty conflicting value wins.

    Citation keys are not paper identifiers. Identifier conflicts prevent merging
    even when titles match. Crossrefs are resolved in their original input file.
    """
    logger = logger or logging.getLogger(__name__)
    groups, parent, index, aliases = [], [], {}, {}

    def root(number):
        while parent[number] != number:
            parent[number] = parent[parent[number]]
            number = parent[number]
        return number

    def compatible(keys_a, keys_b):
        return all(len({v for kind, v in keys_a | keys_b if kind == name}) <= 1
                   for name in ('doi', 'scopus_eid'))

    def enrich(target, other):
        for field, value in other['entry'].items():
            if field == 'ID' or not value.strip():
                continue
            current = target['entry'].get(field, '')
            if not current.strip() or (field == 'ENTRYTYPE' and current == 'misc'):
                target['entry'][field] = value
                target['origins'][field] = other['origins'][field]
            elif current != value and field != 'ENTRYTYPE':
                logger.warning('Conflicting %s for %s; keeping the first value',
                               field, target['entry']['ID'])
        target['keys'].update(other['keys'])

    for source_number, entries in enumerate(sources):
        for original in entries:
            entry = dict(original)
            key = entry['ID']
            keys = identity_keys(entry)
            number = len(groups)
            parent.append(number)
            group = {'entry': entry, 'keys': keys,
                     'origins': {field: source_number for field in entry}}
            groups.append(group)
            # Resolve all matches, including bridge records linking earlier groups.
            candidates = sorted({root(n) for k in keys for n in index.get(k, ())})
            for candidate in candidates:
                candidate = root(candidate)
                number = root(number)
                if candidate == number:
                    continue
                if not compatible(groups[candidate]['keys'], groups[number]['keys']):
                    logger.warning('Identifier conflict for %s and %s; keeping separate records',
                                   groups[candidate]['entry']['ID'], groups[number]['entry']['ID'])
                    continue
                first, later = sorted((candidate, number))
                enrich(groups[first], groups[later])
                parent[later] = first
                number = first
            number = root(number)
            previous = aliases.get((source_number, key))
            if previous is not None and root(previous) != number:
                raise ValueError('Ambiguous citation key within input %d: %s' %
                                 (source_number + 1, key))
            aliases[(source_number, key)] = number
            for identity in groups[number]['keys']:
                index.setdefault(identity, set()).add(number)

    active = [n for n in range(len(groups)) if root(n) == n]
    # Reserve original keys first so suffixes cannot steal a later record's key.
    reserved = {groups[n]['entry']['ID'] for n in active}
    used, final_keys = set(), {}
    for number in active:
        base = groups[number]['entry']['ID']
        key, suffix = base, 2
        while key in used:
            key = '%s_%d' % (base, suffix)
            suffix += 1
            while key in reserved:
                key = '%s_%d' % (base, suffix)
                suffix += 1
        used.add(key)
        final_keys[number] = key

    result = []
    for number in active:
        group = groups[number]
        entry = dict(group['entry'], ID=final_keys[number])
        for field in ('crossref', 'xref'):
            if entry.get(field):
                reference = aliases.get((group['origins'][field], entry[field]))
                if reference is not None:
                    entry[field] = final_keys[root(reference)]
                else:
                    logger.warning('Unresolved %s %s in %s', field, entry[field], entry['ID'])
        result.append(entry)
    return result


def merge_files(inputs, output, logger=None):
    """Parse all inputs before atomically replacing the output; return counts."""
    import bibtexparser
    from bibtexparser.bibdatabase import BibDatabase
    from bibtexparser.bwriter import BibTexWriter

    output = Path(output)
    if any(Path(path).resolve() == output.resolve() for path in inputs):
        raise ValueError('Output must differ from all input files.')
    sources, preambles = [], []
    for path in inputs:
        content = balance_literal_braces(Path(path).read_text(encoding='utf-8-sig'))
        database = parse_bibtex(content, path)
        sources.append(database.entries)
        preambles.extend(p for p in database.preambles if p not in preambles)
    database = BibDatabase()
    database.entries = merge_entries(sources, logger)
    database.preambles = preambles
    writer = BibTexWriter()
    writer.contents = ['preambles', 'entries']
    writer.order_entries_by = None
    content = bibtexparser.dumps(database, writer=writer)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return sum(map(len, sources)), len(database.entries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path, help='Input .bib files, in priority order.')
    parser.add_argument('--output', '-o', required=True, type=Path)
    parser.add_argument('--log-file', type=Path, default=Path('merge_bibtex.log'))
    args = parser.parse_args(argv)
    logger = logging.Logger('merge_bibtex')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    if args.log_file.resolve() in {p.resolve() for p in args.inputs + [args.output]}:
        logger.error('Log file must differ from input and output files.')
        return 1
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open merge log')
        return 1
    log.setFormatter(formatter)
    logger.addHandler(log)
    try:
        total, unique = merge_files(args.inputs, args.output, logger)
        print('Merged %d entries into %d records (%d duplicates merged): %s' %
              (total, unique, total - unique, args.output))
        return 0
    except Exception:
        logger.exception('BibTeX merge failed')
        return 1
    finally:
        log.close()


if __name__ == '__main__':
    sys.exit(main())
