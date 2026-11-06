#!/usr/bin/env python3
"""Open an ACM search in a browser and import a manually exported BibTeX file."""

import argparse
import logging
from pathlib import Path
import sys
from urllib.parse import urlencode
from contextlib import ExitStack
from browser_session import open_search_browser

from bibtex_io import merge_sources, read_bibtex, write_bibtex


def build_search_url(query):
    """Pass the canonical Boolean expression to ACM's web search interface."""
    if not query.strip():
        raise ValueError('The search query must not be empty.')
    return 'https://dl.acm.org/action/doSearch?' + urlencode({'AllField': query.strip()})


def import_bibtex(source, output):
    """Validate a user export, tag every entry with ACM, and atomically save it."""
    source, output = Path(source), Path(output)
    database = read_bibtex(source, require_titles=True)
    for entry in database.entries:
        entry['source'] = merge_sources(entry.get('source', ''), 'ACM')
    write_bibtex(database, output)
    return len(database.entries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    query = parser.add_mutually_exclusive_group(required=True)
    query.add_argument('--query', help='Canonical Boolean search expression.')
    query.add_argument('--query-file', type=Path, help='UTF-8 canonical query file.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--log-file', type=Path, default=Path('acm_search.log'))
    parser.add_argument('--import-bibtex', type=Path,
                        help='Import a completed ACM export instead of opening a browser.')
    parser.add_argument('--no-browser', action='store_true', help='Print the URL without opening it.')
    args = parser.parse_args(argv)
    logger = logging.Logger('acm_search')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    # Check collisions before opening the log so input files cannot be modified.
    protected = [args.output, args.query_file, args.import_bibtex]
    if any(path is not None and path.resolve() == args.log_file.resolve()
           for path in protected):
        logger.error('Log file must differ from the output and input files.')
        return 1
    try:
        file_handler = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open error log %s', args.log_file)
        return 1
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    browsers = ExitStack()
    try:
        if args.query_file and args.query_file.resolve() == args.output.resolve():
            raise ValueError('Output and query file must be different files.')
        expression = (args.query_file.read_text(encoding='utf-8')
                      if args.query_file else args.query).strip()
        url = build_search_url(expression)
        print('Canonical query:\n%s\n\nACM search URL:\n%s' % (expression, url))
        source = args.import_bibtex
        if source is None:
            if not args.no_browser:
                browsers.enter_context(open_search_browser(url))
            print('\nIn ACM, verify the query and result count, then use Export Citations.\n'
                  'Select All results (if available), choose BibTeX, and download the file.\n'
                  'For exports in batches, combine them into one BibTeX file first.\n'
                  'Importing validates the file, not its completeness or query provenance.')
            if not sys.stdin.isatty():
                print('\nSearch prepared; no papers imported. Rerun with '
                      '--import-bibtex /path/to/download.bib to finish.')
                return 0
            path = input('\nDownloaded BibTeX path (Enter to finish later): ').strip()
            if not path:
                print('Search prepared; no papers imported.')
                return 0
            source = Path(path).expanduser()
        if source.resolve() == args.log_file.resolve():
            raise ValueError('The downloaded BibTeX file must differ from the log file.')
        count = import_bibtex(source, args.output)
        print('Imported %d records to %s' % (count, args.output))
        return 0
    except Exception:
        logger.exception('ACM browser search/import failed')
        return 1
    finally:
        browsers.close()
        file_handler.close()


if __name__ == '__main__':
    sys.exit(main())
