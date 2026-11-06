#!/usr/bin/env python3
"""Perform union, intersection, difference, or symmetric difference on BibTeX files."""

import argparse
import logging
from pathlib import Path
import sys
import unicodedata

from pylatexenc.latex2text import LatexNodes2Text
from bibtex_io import merge_sources, new_database, read_bibtex, write_bibtex
from merge_bibtex import normalize_doi


def normalize_text(value):
    """Compare decoded TeX text without differences in case or whitespace."""
    value = LatexNodes2Text().latex_to_text(value)
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def publication_identity(entry):
    return (normalize_doi(entry.get('doi', '')),
            normalize_text(entry.get('title', '')),
            normalize_text(entry.get('author', '')),
            normalize_text(entry.get('abstract', '')))


def _equal(left, right):
    doi_a, title_a, authors_a, abstract_a = left
    doi_b, title_b, authors_b, abstract_b = right
    if doi_a and doi_b:
        return doi_a == doi_b
    return bool(title_a and authors_a and title_a == title_b and authors_a == authors_b
                and (not abstract_a or not abstract_b or abstract_a == abstract_b))


def same_publication(left, right):
    """Use DOI when both have it; otherwise require title/authors and available abstracts.

    Author order is significant. An absent abstract does not prevent a match;
    two present, different abstracts do. Missing title or author prevents fallback.
    """
    return _equal(publication_identity(left), publication_identity(right))


def set_entries(sources, operation, logger=None):
    """Return deduplicated records in first-appearance order.

    Difference means first input minus every later input. Symmetric difference
    across multiple inputs means membership in an odd number of input sets.
    Ambiguous fallback matches never bridge conflicting DOI/abstract identities:
    every member of a group must match the incoming record.
    """
    if operation == 'merge':
        operation = 'union'
    if operation not in ('union', 'intersection', 'difference', 'symmetric-difference'):
        raise ValueError('Unknown set operation: ' + operation)
    sources = list(sources)
    if len(sources) < 2:
        raise ValueError('Set operations require at least two input files.')
    logger = logger or logging.getLogger(__name__)
    groups, index, aliases = [], {}, {}
    for source, entries in enumerate(sources):
        for entry in entries:
            identity = publication_identity(entry)
            doi, title, authors, _ = identity
            keys = ([('doi', doi)] if doi else []) + ([('metadata', title, authors)] if title and authors else [])
            candidates = sorted({n for key in keys for n in index.get(key, ())})
            number = next((n for n in candidates
                           if all(_equal(identity, previous) for previous in groups[n]['identities'])), None)
            if number is None:
                number = len(groups)
                groups.append({'entry': dict(entry), 'identities': [], 'sources': set(),
                               'origins': {field: source for field in entry}})
            else:
                group = groups[number]
                for field, value in entry.items():
                    if field == 'ID' or not value.strip():
                        continue
                    current = group['entry'].get(field, '')
                    if field == 'source':
                        group['entry'][field] = merge_sources(current, value)
                    elif not current.strip() or (field == 'ENTRYTYPE' and current == 'misc'):
                        group['entry'][field] = value
                        group['origins'][field] = source
                    elif current != value:
                        logger.warning('Conflicting %s for %s; keeping first value', field, group['entry']['ID'])
            group = groups[number]
            group['identities'].append(identity)
            group['sources'].add(source)
            alias = (source, entry['ID'])
            if alias in aliases and aliases[alias] != number:
                raise ValueError('Ambiguous citation key in input %d: %s' % (source + 1, entry['ID']))
            aliases[alias] = number
            for key in keys:
                index.setdefault(key, set()).add(number)
    def keep(group):
        members = group['sources']
        return (operation == 'union'
                or operation == 'intersection' and len(members) == len(sources)
                or operation == 'difference' and members == {0}
                or operation == 'symmetric-difference' and len(members) % 2 == 1)
    selected = [n for n, group in enumerate(groups) if keep(group)]
    reserved = {groups[n]['entry']['ID'] for n in selected}
    used, final_keys = set(), {}
    for n in selected:
        base = groups[n]['entry']['ID']
        key, suffix = base, 2
        while key in used or (key != base and key in reserved):
            key = '%s_%d' % (base, suffix)
            suffix += 1
        used.add(key)
        final_keys[n] = key
    result = []
    for n in selected:
        group = groups[n]
        entry = dict(group['entry'], ID=final_keys[n])
        if 'source' in entry:
            entry['source'] = merge_sources(entry['source'])
        for field in ('crossref', 'xref'):
            if entry.get(field):
                target = aliases.get((group['origins'][field], entry[field]))
                if target in final_keys:
                    entry[field] = final_keys[target]
                else:
                    logger.warning('Unresolved or excluded %s %s in %s', field, entry[field], entry['ID'])
        result.append(entry)
    return result


def set_files(inputs, output, operation, logger=None):
    inputs, output = [Path(path) for path in inputs], Path(output)
    if any(path.resolve() == output.resolve()
           or (output.exists() and path.samefile(output)) for path in inputs):
        raise ValueError('Output must differ from all input files.')
    databases = [read_bibtex(path) for path in inputs]
    entries = set_entries([db.entries for db in databases], operation, logger)
    preambles = list(dict.fromkeys(p for db in databases for p in db.preambles))
    write_bibtex(new_database(entries, preambles), output)
    return sum(len(db.entries) for db in databases), len(entries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['union', 'merge', 'intersection', 'difference', 'symmetric-difference'])
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('--output', '-o', required=True, type=Path)
    parser.add_argument('--log-file', type=Path, default=Path('bibtex_sets.log'))
    args = parser.parse_args(argv)
    if len(args.inputs) < 2:
        parser.error('Provide at least two input files.')
    logger = logging.Logger('bibtex_sets')
    logger.addHandler(logging.StreamHandler(sys.stderr))
    if any(args.log_file.resolve() == path.resolve()
           or (args.log_file.exists() and path.exists() and args.log_file.samefile(path))
           for path in args.inputs + [args.output]):
        logger.error('Log file must differ from input and output files.')
        return 1
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open set-operation log')
        return 1
    logger.addHandler(log)
    try:
        total, count = set_files(args.inputs, args.output, args.operation, logger)
        print('Read %d entries; saved %d records: %s' % (total, count, args.output))
        return 0
    except Exception:
        logger.exception('BibTeX set operation failed')
        return 1
    finally:
        log.close()


if __name__ == '__main__':
    sys.exit(main())
