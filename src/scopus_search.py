#!/usr/bin/env python3
"""Search Scopus using pybliometrics and export search metadata to BibTeX."""

import argparse
import logging
from pathlib import Path
import re
import sys

from bibtex_io import escape_bibtex, new_database, serialize_bibtex, write_bibtex


def record_to_entry(record, index):
    """Convert a ScopusSearch Document without additional per-paper API calls."""
    def value(name):
        return getattr(record, name, None)

    subtype = value('subtype')
    entry_type = {'ar': 'article', 're': 'article', 'cp': 'inproceedings',
                  'ch': 'incollection', 'bk': 'book'}.get(subtype, 'misc')
    identifier = value('eid') or value('doi') or 'result'
    key = 'scopus_' + re.sub(r'[^A-Za-z0-9_-]', '_', str(identifier)) + '_' + str(index)
    fields = {'source': 'Scopus'}
    if value('author_names'):
        fields['author'] = ' and '.join(
            escape_bibtex(author.strip()) for author in value('author_names').split(';')
            if author.strip()
        )
    elif value('creator'):
        fields['author'] = escape_bibtex(value('creator'))

    mapping = {
        'title': 'title', 'doi': 'doi', 'volume': 'volume',
        'issueIdentifier': 'number', 'description': 'abstract',
        'authkeywords': 'keywords', 'issn': 'issn', 'eid': 'scopus_eid',
        'article_number': 'eid',
    }
    publication_field = {'article': 'journal', 'inproceedings': 'booktitle',
                         'incollection': 'booktitle'}.get(entry_type, 'series')
    mapping['publicationName'] = publication_field
    for source, target in mapping.items():
        item = value(source)
        if item is not None and str(item).strip():
            fields[target] = escape_bibtex(item)
    if value('coverDate'):
        year = re.match(r'\d{4}', str(value('coverDate')))
        if year:
            fields['year'] = year.group()
    if value('pageRange'):
        fields['pages'] = escape_bibtex(re.sub(r'(?<=\d)-(?=\d)', '--', value('pageRange')))
    return dict(fields, ENTRYTYPE=entry_type, ID=key)


def record_to_bibtex(record, index):
    """Serialize a Scopus record using python-bibtexparser."""
    return serialize_bibtex(new_database([record_to_entry(record, index)]))


def search_to_bibtex(query, output, *, refresh=False, subscriber=True, config_path=None):
    """Retrieve all results and atomically save them; propagate errors to caller."""
    if not query.strip():
        raise ValueError('The search query must not be empty.')
    import pybliometrics
    from pybliometrics.scopus import ScopusSearch

    pybliometrics.init(**({'config_path': config_path} if config_path else {}))
    search = ScopusSearch(query, refresh=refresh, subscriber=subscriber,
                          download=True, verbose=False)
    records = search.results or []
    database = new_database(record_to_entry(record, index)
                            for index, record in enumerate(records, start=1))
    write_bibtex(database, output)
    return len(records)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--query', help='Scopus API search expression.')
    source.add_argument('--query-file', type=Path, help='UTF-8 file containing the query.')
    parser.add_argument('--output', type=Path, required=True, help='Destination .bib file.')
    parser.add_argument('--log-file', type=Path, default=Path('scopus_search.log'))
    parser.add_argument('--config-path', type=Path, help='pybliometrics configuration file.')
    parser.add_argument('--refresh', action='store_true', help='Refresh cached results.')
    parser.add_argument('--non-subscriber', action='store_true',
                        help='Use STANDARD metadata and non-subscriber API limits.')
    args = parser.parse_args(argv)
    logger = logging.Logger('scopus_search')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    try:
        file_handler = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open error log %s', args.log_file)
        return 1
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    try:
        if args.output.resolve() == args.log_file.resolve():
            raise ValueError('Output and log file must be different files.')
        if args.query_file and args.output.resolve() == args.query_file.resolve():
            raise ValueError('Output and query file must be different files.')
        query = args.query_file.read_text(encoding='utf-8') if args.query_file else args.query
        count = search_to_bibtex(query.strip(), args.output, refresh=args.refresh,
                                subscriber=not args.non_subscriber,
                                config_path=args.config_path)
        print('Saved %d records to %s' % (count, args.output))
        return 0
    except Exception:
        logger.exception('Scopus search/export failed')
        return 1
    finally:
        file_handler.close()


if __name__ == '__main__':
    sys.exit(main())
