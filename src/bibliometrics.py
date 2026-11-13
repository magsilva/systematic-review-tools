#!/usr/bin/env python3
"""Report BibTeX publication statistics and export coauthor and citation graphs."""

import argparse
from collections import Counter, defaultdict
from itertools import combinations
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET

from bibtexparser.middlewares.names import parse_single_name_into_parts, split_multiple_persons_names
from pylatexenc.latex2text import LatexNodes2Text

from bibliometrics_html import REPORT_NAME, ranking, write_html_report
from bibtex_interrupt import bibtex_checkpoint, interrupt_exit, track_bibtex
from bibtex_io import new_database, read_bibtex
from citation_metadata import doi_of

LATEX = LatexNodes2Text()
REFERENCE_FIELDS = ('references', 'cites', 'cited-references', 'cited_references', 'referenced_works')
CONFERENCE_TYPES = {'conference', 'inproceedings', 'proceedings'}
GRAPHML_NS = 'http://graphml.graphdrawing.org/xmlns'


def plain(value):
    # Metadata can contain literal percent signs, which TeX treats as comments.
    value = re.sub(r'(\\*)%',
                   lambda match: match[1] + ('\\' if len(match[1]) % 2 == 0 else '') + '%',
                   value)
    return ' '.join(LATEX.latex_to_text(value).split())


def folded(value):
    return unicodedata.normalize('NFKC', value).casefold()


def authors(value):
    for name in split_multiple_persons_names(value):
        parts = parse_single_name_into_parts(name, strict=False)
        label = plain(parts.merge_first_name_first)
        if label and folded(label) != 'others':
            yield label


def publication_year(entry):
    year = plain(entry.get('year', ''))
    if re.fullmatch(r'[0-9]{4}', year):
        return year
    match = re.match(r'([0-9]{4})(?:-|$)', plain(entry.get('date', '')))
    return match[1] if match else 'Unknown'


class PublicationStats:
    def __init__(self):
        self.years = Counter()
        self.events = Counter()
        self.journals = Counter()
        self.authors = Counter()
        self.collaborations = Counter()
        self.labels = {}

    def label_key(self, label):
        key = folded(label)
        self.labels.setdefault(key, label)
        return key

    def add(self, entry):
        self.years[publication_year(entry)] += 1
        journal = plain(entry.get('journal') or entry.get('journaltitle', ''))
        event = plain(entry.get('eventtitle', ''))
        if not event and entry['ENTRYTYPE'] in CONFERENCE_TYPES:
            event = plain(entry.get('booktitle', ''))
        if journal:
            self.journals[self.label_key(journal)] += 1
        if event:
            self.events[self.label_key(event)] += 1
        keys = sorted({self.label_key(name) for name in authors(entry.get('author', ''))})
        self.authors.update(keys)
        self.collaborations.update(combinations(keys, 2))


def identifiers(value):
    """Read a list of exact identifiers separated by semicolons, commas or newlines."""
    return (item.strip().strip('{}').strip() for item in re.split(r'[;,\n]', value) if item.strip())


def identifier_key(value):
    doi = doi_of({'doi': value})
    if doi:
        return 'doi', doi
    identifier = value.removeprefix('https://openalex.org/').upper()
    if re.fullmatch(r'W[0-9]+', identifier):
        return 'openalex', identifier
    return 'key', value


class CitationGraph:
    def __init__(self, entries):
        self.index = defaultdict(set)
        self.edges = set()
        self.dois = {}
        for entry in entries:
            key = entry['ID']
            self.index['key', key].add(key)
            doi = doi_of(entry)
            self.dois[key] = doi
            if doi:
                self.index['doi', doi].add(key)
            if entry.get('openalex_id'):
                self.index[identifier_key(entry['openalex_id'])].add(key)

    def resolve(self, value):
        return self.unique(('key', value)) or self.unique(identifier_key(value))

    def unique(self, identifier):
        matches = self.index.get(identifier)
        return next(iter(matches)) if matches and len(matches) == 1 else None

    def resolve_entry(self, entry):
        doi = doi_of(entry)
        key = self.unique(('doi', doi)) if doi else None
        if key is None and entry.get('openalex_id'):
            key = self.unique(identifier_key(entry['openalex_id']))
        if key is None:
            key = self.unique(('key', entry['ID']))
        if key is not None and (not doi or not self.dois[key] or self.dois[key] == doi):
            return key
        return None

    def add(self, citing, cited):
        if citing and cited and citing != cited:
            self.edges.add((citing, cited))

    def add_bibtex_references(self, entries):
        for entry in entries:
            for field in REFERENCE_FIELDS:
                for reference in identifiers(entry.get(field, '')):
                    self.add(entry['ID'], self.resolve(reference))

    def add_citation_export(self, entries, *, forward):
        for entry in entries:
            key = self.resolve_entry(entry)
            if key is None:
                continue
            for source in identifiers(entry.get('source', '')):
                if source.casefold() in {'acm', 'scopus', 'sol', 'google scholar'}:
                    continue
                other = self.resolve(source)
                if forward:
                    self.add(key, other)
                else:
                    self.add(other, key)

    def add_crossref_references(self, entries, client):
        failures = 0
        for entry in entries:
            doi = doi_of(entry)
            if not doi:
                continue
            try:
                work = client.work(doi)
            except (OSError, ValueError, KeyError) as error:
                print(f'Crossref lookup failed for {doi}: {error}', file=sys.stderr)
                failures += 1
                continue
            for reference in (work or {}).get('reference') or []:
                target = doi_of({'doi': reference.get('DOI')})
                if target:
                    self.add(entry['ID'], self.unique(('doi', target)))
        return failures


def write_graph(path, *, directed, node_fields, edge_fields, nodes, edges):
    """Write GraphML with typed attributes and stable internal node IDs."""
    root = ET.Element('graphml', xmlns=GRAPHML_NS)
    for scope, fields in (('node', node_fields), ('edge', edge_fields)):
        for name, kind in fields.items():
            ET.SubElement(root, 'key', {'id': f'{scope}_{name}', 'for': scope,
                                       'attr.name': name, 'attr.type': kind})
    graph = ET.SubElement(root, 'graph', edgedefault='directed' if directed else 'undirected')
    node_ids = {}
    for index, (key, fields) in enumerate(nodes):
        node_ids[key] = node_id = f'n{index}'
        node = ET.SubElement(graph, 'node', id=node_id)
        for name, value in fields.items():
            ET.SubElement(node, 'data', key=f'node_{name}').text = str(value)
    for source, target, fields in edges:
        edge = ET.SubElement(graph, 'edge', source=node_ids[source], target=node_ids[target])
        for name, value in fields.items():
            ET.SubElement(edge, 'data', key=f'edge_{name}').text = str(value)
    ET.indent(root)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            ET.ElementTree(root).write(stream, encoding='utf-8', xml_declaration=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_graphs(directory, stats, entries, citations):
    write_graph(
        directory / 'coauthors.graphml', directed=False,
        node_fields={'label': 'string', 'papers': 'int'}, edge_fields={'weight': 'int'},
        nodes=((key, {'label': stats.labels[key], 'papers': count})
               for key, count in sorted(stats.authors.items())),
        edges=((left, right, {'weight': count})
               for (left, right), count in sorted(stats.collaborations.items())),
    )
    write_graph(
        directory / 'citations.graphml', directed=True,
        node_fields={'label': 'string', 'bibtex_key': 'string', 'year': 'string', 'doi': 'string'},
        edge_fields={},
        nodes=((entry['ID'], {'label': plain(entry.get('title', '')) or entry['ID'],
                              'bibtex_key': entry['ID'], 'year': publication_year(entry),
                              'doi': doi_of(entry)}) for entry in entries),
        edges=((citing, cited, {}) for citing, cited in sorted(citations.edges)),
    )


def print_report(stats, citations, directory, top):
    print(f'Papers analyzed: {sum(stats.years.values())}')
    print('\nPapers per year')
    for year, count in sorted(stats.years.items()):
        print(f'  {year}: {count}')
    for heading, counts in (('Top events', stats.events), ('Top journals', stats.journals),
                            ('Top authors', stats.authors)):
        print(f'\n{heading}')
        ranked = ranking(counts, stats.labels, top)
        for label, count in ranked:
            print(f'  {count:>5}  {label}')
        if not ranked:
            print('  No data available')
    print(f'\nCoauthor graph: {len(stats.authors)} authors, {len(stats.collaborations)} links'
          f' → {directory / "coauthors.graphml"}')
    print(f'Citation graph: {sum(stats.years.values())} papers, {len(citations.edges)} links'
          f' → {directory / "citations.graphml"}')
    if not citations.edges:
        print('No citations between input papers were found in the available data.')


@interrupt_exit
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='BibTeX bibliography to analyze.')
    parser.add_argument('--output-dir', type=Path, default=Path('bibliometrics'))
    parser.add_argument('--top', type=int, default=10, help='Rows per ranking (default: 10).')
    parser.add_argument('--cited-bibtex', action='append', type=Path, default=[],
                        help='Output of extract_references.py. May be repeated.')
    parser.add_argument('--citing-bibtex', action='append', type=Path, default=[],
                        help='Output of extract_citing_papers.py. May be repeated.')
    parser.add_argument('--crossref', action='store_true',
                        help='Look up references for DOI-bearing papers using Crossref.')
    parser.add_argument('--mailto', help='Optional contact email for Crossref requests.')
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error('--top must be positive.')
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name in ('coauthors.graphml', 'citations.graphml', REPORT_NAME):
            output = args.output_dir / name
            for source in (args.input, *args.cited_bibtex, *args.citing_bibtex):
                if output.resolve() == source.resolve() or (
                        output.exists() and source.exists() and output.samefile(source)):
                    raise ValueError(f'Report destination must differ from input {source}.')
        with bibtex_checkpoint(args.output_dir / 'bibliometrics.bib'):
            database = read_bibtex(args.input)
            stats = PublicationStats()
            completed = new_database(preambles=database.preambles)
            track_bibtex(lambda: completed)
            seen = set()
            for entry in database.entries:
                if entry['ID'] in seen:
                    raise ValueError(f'Duplicate BibTeX key: {entry["ID"]}')
                seen.add(entry['ID'])
                stats.add(entry)
                completed.entries.append(entry)
            citations = CitationGraph(completed.entries)
            citations.add_bibtex_references(completed.entries)
            for path in args.cited_bibtex:
                citations.add_citation_export(read_bibtex(path).entries, forward=False)
            for path in args.citing_bibtex:
                citations.add_citation_export(read_bibtex(path).entries, forward=True)
            failures = 0
            if args.crossref:
                import requests
                from extract_references import Crossref
                with requests.Session() as session:
                    session.headers['User-Agent'] = 'systematic-review-tools/bibliometrics'
                    failures = citations.add_crossref_references(
                        completed.entries, Crossref(session, mailto=args.mailto))
            export_graphs(args.output_dir, stats, completed.entries, citations)
            report = args.output_dir / REPORT_NAME
            papers = {entry['ID']: (plain(entry.get('title', '')) or entry['ID'],
                                    f'{entry["ID"]} · {publication_year(entry)}')
                      for entry in completed.entries}
            write_html_report(
                report, args.input, stats, papers,
                [(left, right, weight) for (left, right), weight in sorted(stats.collaborations.items())],
                [(left, right, 1) for left, right in sorted(citations.edges)], args.top,
            )
            print_report(stats, citations, args.output_dir, args.top)
            print(f'Web report: {report}')
        return int(bool(failures))
    except (OSError, ValueError) as error:
        print(f'Bibliometrics failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
