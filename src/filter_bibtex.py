#!/usr/bin/env python3
"""Filter BibTeX entries using inclusion and exclusion criteria."""

from abc import ABC, abstractmethod
import argparse
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import sys

from bibtex_interrupt import bibtex_checkpoint, interrupt_exit, track_bibtex
from bibtex_io import new_database, read_bibtex, write_bibtex
from pico_search import add_pico_arguments


class Criterion(ABC):
    """Implement accepts(entry) to add another independent filtering criterion."""

    @abstractmethod
    def accepts(self, entry):
        pass


@dataclass(frozen=True)
class EntryTypeCriterion(Criterion):
    include_types: frozenset = None
    exclude_types: frozenset = frozenset()

    def __post_init__(self):
        if self.include_types is not None:
            object.__setattr__(self, 'include_types',
                               frozenset(name.strip().casefold() for name in self.include_types))
        object.__setattr__(self, 'exclude_types',
                           frozenset(name.strip().casefold() for name in self.exclude_types))

    def accepts(self, entry):
        entry_type = entry.get('ENTRYTYPE', '').strip().casefold()
        return (entry_type not in self.exclude_types
                and (self.include_types is None or entry_type in self.include_types))


class PicoCriterion(Criterion):
    """OR terms across title/abstract/keywords, AND supplied PICO components.

    Matching uses case-insensitive literal substrings, as in the mapping filter.
    Empty components impose no restriction. Metadata is never modified.
    """

    def __init__(self, population=(), intervention=(), control=(), outcome=()):
        self.components = {
            name: tuple(term.strip().casefold() for term in terms if term.strip())
            for name, terms in zip(
                ('population', 'intervention', 'control', 'outcome'),
                (population, intervention, control, outcome))
        }

    def accepts(self, entry):
        if not any(self.components.values()):
            return True
        from pylatexenc.latex2text import LatexNodes2Text
        decoder = LatexNodes2Text()
        def searchable_text(value):
            # Bibliographic metadata uses percent signs as text, not TeX comments.
            # Preserve already-escaped signs and handle even backslash runs too.
            value = re.sub(r'(\\*)%',
                           lambda match: match[1] + ('\\' if len(match[1]) % 2 == 0 else '') + '%',
                           value)
            return decoder.latex_to_text(value).casefold()

        fields = [searchable_text(entry.get(field, ''))
                  for field in ('title', 'abstract', 'keywords')]
        return all(any(term in text for term in terms for text in fields)
                   for terms in self.components.values() if terms)


def filter_entries(entries, criteria):
    """Keep entries satisfying every criterion, preserving their order and metadata."""
    return list(iter_matches(entries, criteria))


def iter_matches(entries, criteria):
    criteria = tuple(criteria)
    return (dict(entry) for entry in entries if all(rule.accepts(entry) for rule in criteria))


def filter_file(source, output, criteria, logger=None):
    """Read strictly, evaluate criteria, and atomically save to a different file."""
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
        raise ValueError('Output must be a different file from the input.')
    with bibtex_checkpoint(output):
        database = read_bibtex(source)
        original = database.entries
        database = new_database(preambles=database.preambles)
        track_bibtex(lambda: database)
        database.entries.extend(iter_matches(original, criteria))
        removed = {entry['ID'] for entry in original} - {entry['ID'] for entry in database.entries}
        if logger is not None:
            for entry in database.entries:
                for field in ('crossref', 'xref'):
                    if entry.get(field) in removed:
                        logger.warning('%s refers to filtered-out entry %s through %s',
                                       entry['ID'], entry[field], field)
        write_bibtex(database, output)
        return len(original), len(database.entries)


def entry_type_name(value):
    value = value.strip().casefold()
    if not re.fullmatch(r'[a-z][a-z0-9_-]*', value):
        raise argparse.ArgumentTypeError('Specify an entry type such as article, inproceedings, or book.')
    return value


@interrupt_exit
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', '-o', type=Path, required=True)
    parser.add_argument('--include-type', nargs='+', action='extend', type=entry_type_name,
                        metavar='TYPE', help='Keep only these types; may be repeated.')
    parser.add_argument('--exclude-type', nargs='+', action='extend', type=entry_type_name,
                        default=[], metavar='TYPE', help='Exclude these types; overrides inclusion; may be repeated.')
    parser.add_argument('--log-file', type=Path, default=Path('filter_bibtex.log'))
    add_pico_arguments(parser)
    args = parser.parse_args(argv)
    logger = logging.Logger('filter_bibtex')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    if any(args.log_file.resolve() == path.resolve()
           or (args.log_file.exists() and path.exists() and args.log_file.samefile(path))
           for path in (args.input, args.output)):
        logger.error('Log file must differ from the input and output files.')
        return 1
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open filter log')
        return 1
    log.setFormatter(formatter)
    logger.addHandler(log)
    try:
        criteria = [EntryTypeCriterion(args.include_type, args.exclude_type),
                    PicoCriterion(args.population, args.intervention, args.control, args.outcome)]
        total, kept = filter_file(args.input, args.output, criteria, logger)
        print('Read %d entries; kept %d; excluded %d. BibTeX: %s' %
              (total, kept, total - kept, args.output))
        return 0
    except Exception:
        logger.exception('BibTeX filtering failed')
        return 1
    finally:
        log.close()


if __name__ == '__main__':
    sys.exit(main())
