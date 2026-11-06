#!/usr/bin/env python3
"""Generate a PICO search query, run the search, and export papers to BibTeX."""

import argparse
from pathlib import Path
import sys

from pico_search import add_pico_arguments, build_search_expression
import scopus_search
import acm_search


def run_scopus(args, keywords):
    """Adapt PICO keywords and console options to the existing Scopus runner."""
    query = build_search_expression(
        **keywords, output_format='scopus', clause_prefix='TITLE-ABS-KEY'
    )
    options = ['--query', query, '--output', str(args.output),
               '--log-file', str(args.log_file)]
    if args.config_path is not None:
        options.extend(['--config-path', str(args.config_path)])
    if args.refresh:
        options.append('--refresh')
    if args.non_subscriber:
        options.append('--non-subscriber')
    return scopus_search.main(options)


def run_acm(args, keywords):
    """Send the canonical expression to the browser-assisted ACM runner."""
    query = build_search_expression(**keywords)
    options = ['--query', query, '--output', str(args.output),
               '--log-file', str(args.log_file)]
    if args.import_bibtex is not None:
        options.extend(['--import-bibtex', str(args.import_bibtex)])
    if args.no_browser:
        options.append('--no-browser')
    return acm_search.main(options)


# Register an adapter here to expose another search engine through the console.
SEARCH_ENGINES = {'scopus': run_scopus, 'acm': run_acm}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=str.lower, choices=SEARCH_ENGINES, required=True,
                        help='Search engine to use.')
    add_pico_arguments(parser)
    parser.add_argument('--output', type=Path, required=True, help='Destination .bib file.')
    parser.add_argument('--log-file', type=Path, help='Default: <engine>_search.log.')
    parser.add_argument('--import-bibtex', type=Path, help='ACM: import a downloaded export.')
    parser.add_argument('--no-browser', action='store_true', help='ACM: only print the search URL.')
    parser.add_argument('--config-path', type=Path, help='pybliometrics configuration file.')
    parser.add_argument('--refresh', action='store_true', help='Refresh cached results.')
    parser.add_argument('--non-subscriber', action='store_true',
                        help='Use Scopus non-subscriber access.')
    args = parser.parse_args(argv)
    if args.engine == 'scopus' and (args.import_bibtex is not None or args.no_browser):
        parser.error('--import-bibtex and --no-browser apply only to ACM.')
    if args.engine == 'acm' and (args.config_path is not None or args.refresh or args.non_subscriber):
        parser.error('--config-path, --refresh and --non-subscriber apply only to Scopus.')
    if args.log_file is None:
        args.log_file = Path(args.engine + '_search.log')
    keywords = {component: getattr(args, component)
                for component in ('population', 'intervention', 'control', 'outcome')}
    try:
        return SEARCH_ENGINES[args.engine](args, keywords)
    except ValueError as error:
        parser.error(str(error))


if __name__ == '__main__':
    sys.exit(main())
