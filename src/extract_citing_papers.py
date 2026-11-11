#!/usr/bin/env python3
"""Export papers that cite the works in a BibTeX file using OpenAlex."""

import argparse
from collections import Counter
import logging
from pathlib import Path
import sys

import requests

from bibtex_interrupt import bibtex_checkpoint, interrupt_exit, track_bibtex
from bibtex_io import merge_sources, new_database, read_bibtex, write_bibtex
from citation_metadata import doi_of
from openalex import OpenAlex, citation_entry, configure_session, work_id


def extract(entries, client, logger):
    records, stats = {}, Counter()
    track_bibtex(lambda: new_database(records.values()))
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
                stored = dict(records[key]) if key in records else dict(
                    entry, ID='citing_%d' % (len(records) + 1))
                for field, value in entry.items():
                    stored.setdefault(field, value)
                stored['source'] = merge_sources(stored.get('source', ''), source)
                records[key] = stored
            stats['completed'] += 1
        except (requests.RequestException, ValueError, KeyError) as error:
            logger.warning('%s: %s', source, error)
            stats['errors'] += 1
    return list(records.values()), stats


@interrupt_exit
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
        with bibtex_checkpoint(args.output):
            entries = read_bibtex(args.input).entries
            with requests.Session() as session:
                configure_session(session)
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
