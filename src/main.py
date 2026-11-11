#!/usr/bin/env python3
"""Search or load BibTeX, optionally filter papers and download their PDFs."""

import argparse
import copy
import os
import tempfile
from pathlib import Path
import sys

from pico_search import add_pico_arguments, build_search_expression
import scopus_search
import acm_search
import sol_search
import filter_bibtex
import download_pdfs


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


def run_sol(args, keywords):
    """Send the canonical PICO expression to SOL's automated browser runner."""
    options = ['--query', build_search_expression(**keywords),
               '--output', str(args.output), '--log-file', str(args.log_file),
               '--timeout', str(args.timeout)]
    if args.show_browser:
        options.append('--show-browser')
    return sol_search.main(options)


# Register an adapter here to expose another search engine through the console.
SEARCH_ENGINES = {'scopus': run_scopus, 'acm': run_acm, 'sol': run_sol}


def run_pipeline(args, keywords):
    """Stage search/filter outputs; download metadata is saved by the downloader."""
    processing = args.input is not None or args.filter_pico or args.include_type or args.exclude_type or args.download_pdfs
    if not processing:
        return SEARCH_ENGINES[args.engine](args, keywords)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
        directory = Path(directory)
        current = args.input
        if current is None:
            search_args = copy.copy(args)
            search_args.output = directory / 'search.bib'
            status = SEARCH_ENGINES[args.engine](search_args, keywords)
            if status:
                return status
            if not search_args.output.exists():
                print('Search prepared; filtering and downloads await a BibTeX import.')
                return 0
            current = search_args.output
        filtered = directory / 'filtered.bib'
        options = [str(current), '--output', str(filtered), '--log-file', str(args.log_file)]
        for name in ('include_type', 'exclude_type'):
            if getattr(args, name):
                options += ['--' + name.replace('_', '-'), *getattr(args, name)]
        if args.filter_pico:
            for component, terms in keywords.items():
                if terms:
                    options += ['--' + component, *terms]
        status = filter_bibtex.main(options)
        if status:
            return status
        if args.download_pdfs:
            options = [str(filtered), '--output', str(args.output),
                       '--pdf-dir', str(args.pdf_dir), '--log-file', str(args.log_file),
                       '--rate-limit', str(args.rate_limit), '--timeout', str(args.timeout),
                       '--max-retries', str(args.max_retries),
                       '--browser-profile', str(args.browser_profile)]
            for name in ('source', 'exclude_source'):
                if getattr(args, name):
                    options += ['--' + name.replace('_', '-'), *getattr(args, name)]
            for name in ('show_browser', 'no_progress'):
                if getattr(args, name):
                    options.append('--' + name.replace('_', '-'))
            return download_pdfs.main(options)
        os.replace(filtered, args.output)
        print('Saved processed bibliography to %s' % args.output)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Process an existing bibliography instead of searching.')
    source.add_argument('--engine', type=str.lower, choices=SEARCH_ENGINES,
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
    parser.add_argument('--include-type', nargs='+', action='extend', type=filter_bibtex.entry_type_name,
                        help='Keep only these paper types (e.g. article inproceedings incollection book).')
    parser.add_argument('--exclude-type', nargs='+', action='extend', type=filter_bibtex.entry_type_name)
    parser.add_argument('--filter-pico', action='store_true',
                        help='Require a title/abstract/keywords match for each supplied PICO component.')
    parser.add_argument('--download-pdfs', action='store_true', help='Download PDFs after filtering.')
    parser.add_argument('--pdf-dir', type=Path, default=Path('pdfs'))
    parser.add_argument('--rate-limit', type=download_pdfs.positive_number, default=2)
    parser.add_argument('--timeout', type=download_pdfs.positive_number, default=30)
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--source', nargs='+', action='extend', type=download_pdfs.source_name)
    parser.add_argument('--exclude-source', nargs='+', action='extend', type=download_pdfs.source_name)
    parser.add_argument('--browser-profile', type=Path, default=Path('.pdf-browser'))
    parser.add_argument('--show-browser', action='store_true')
    parser.add_argument('--no-progress', action='store_true')
    args = parser.parse_args(argv)
    if args.max_retries < 0:
        parser.error('--max-retries must not be negative.')
    if args.input is not None and (args.import_bibtex or args.no_browser or args.config_path or args.refresh or args.non_subscriber):
        parser.error('Search-specific options cannot be used with --input.')
    if args.engine != 'acm' and (args.import_bibtex is not None or args.no_browser):
        parser.error('--import-bibtex and --no-browser apply only to ACM.')
    if args.engine != 'scopus' and (args.config_path is not None or args.refresh or args.non_subscriber):
        parser.error('--config-path, --refresh and --non-subscriber apply only to Scopus.')
    if args.log_file is None:
        args.log_file = Path(args.engine + '_search.log' if args.engine else 'review_pipeline.log')
    keywords = {component: getattr(args, component)
                for component in ('population', 'intervention', 'control', 'outcome')}
    protected = [args.output] + [path for path in (args.input, args.import_bibtex) if path is not None]
    if any(args.log_file.resolve() == path.resolve()
           or (args.log_file.exists() and path.exists() and args.log_file.samefile(path)) for path in protected):
        parser.error('Log file must differ from all BibTeX inputs and output.')
    if args.input is not None and (args.input.resolve() == args.output.resolve()
            or (args.output.exists() and args.input.samefile(args.output))):
        parser.error('Output must differ from the input file.')
    try:
        if args.engine is not None or args.filter_pico:
            build_search_expression(**keywords)  # Validate before performing any work.
        return run_pipeline(args, keywords)
    except ValueError as error:
        parser.error(str(error))
    except OSError as error:
        print('Pipeline failed: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
