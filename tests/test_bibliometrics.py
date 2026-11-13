import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from bibliometrics import CitationGraph, GRAPHML_NS, main


def read_graph(path, attribute):
    root = ET.parse(path).getroot()
    namespace = {'g': GRAPHML_NS}
    graph = root.find('g:graph', namespace)
    nodes = {}
    for node in graph.findall('g:node', namespace):
        metadata = {data.get('key'): data.text for data in node.findall('g:data', namespace)}
        nodes[node.get('id')] = metadata
    edges = {}
    for edge in graph.findall('g:edge', namespace):
        left = nodes[edge.get('source')][attribute]
        right = nodes[edge.get('target')][attribute]
        weight = edge.find('g:data', namespace)
        edges[left, right] = int(weight.text) if weight is not None else None
    return graph.get('edgedefault'), list(nodes.values()), edges


class BibliometricsTests(unittest.TestCase):
    def test_cli_counts_and_graphs_with_both_citation_export_directions(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / 'papers.bib'
            source.write_text(r'''
@inproceedings{a, title={First}, year={2024}, booktitle={Proceedings},
  eventtitle={Example Event}, doi={10.1234/first},
  author={Doe, Jane and Garc{\'i}a, Ana}, references={b; 10.1234/second; external}}
@article{b, title={Second}, date={2025-03}, journaltitle={Example Journal},
  doi={10.1234/second}, author={Ana García and Jane Doe and Jane Doe and others}}
@article{c, title={Third}, year={2025}, journal={EXAMPLE JOURNAL},
  author={{Research and Development}}, openalex_id={W123}}
@misc{d, title={Fourth}, author={Solo Author}}
''', encoding='utf-8')
            cited = directory / 'cited.bib'
            cited.write_text('@article{ref, doi={https://doi.org/10.1234/SECOND}, source={c}}')
            citing = directory / 'citing.bib'
            citing.write_text('@article{forward, openalex_id={W123}, source={10.1234/first}}')
            report = io.StringIO()
            with contextlib.redirect_stdout(report):
                status = main([str(source), '--output-dir', str(directory / 'graphs'),
                               '--cited-bibtex', str(cited), '--citing-bibtex', str(citing)])
            self.assertEqual(status, 0)
            for expected in ('2024: 1', '2025: 2', 'Unknown: 1', '1  Example Event',
                             '2  Example Journal', '2  Jane Doe'):
                self.assertIn(expected, report.getvalue())

            kind, nodes, edges = read_graph(directory / 'graphs/coauthors.graphml', 'node_label')
            self.assertEqual(kind, 'undirected')
            self.assertEqual({n['node_label']: n['node_papers'] for n in nodes},
                             {'Ana García': '2', 'Jane Doe': '2', 'Research and Development': '1',
                              'Solo Author': '1'})
            self.assertEqual(edges, {('Ana García', 'Jane Doe'): 2})

            kind, nodes, edges = read_graph(directory / 'graphs/citations.graphml', 'node_bibtex_key')
            self.assertEqual(kind, 'directed')
            self.assertEqual({n['node_bibtex_key'] for n in nodes}, {'a', 'b', 'c', 'd'})
            self.assertEqual(set(edges), {('a', 'b'), ('c', 'a'), ('c', 'b')})

    def test_crossref_links_only_known_dois_and_preserves_links_on_lookup_failure(self):
        entries = [{'ID': 'a', 'doi': '10.1234/first'},
                   {'ID': 'b', 'doi': '10.1234/second'}, {'ID': 'c'}]
        citations = CitationGraph(entries)
        client = Mock()
        client.work.side_effect = [
            {'reference': [{'DOI': '10.1234/SECOND'}, {'DOI': '10.1234/first'},
                           {'DOI': '10.1234/external'}, {'unstructured': 'A reference'}]},
            OSError('Unavailable'),
        ]
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(citations.add_crossref_references(entries, client), 1)
        self.assertIn('10.1234/second', stderr.getvalue())
        self.assertEqual(citations.edges, {('a', 'b')})
        self.assertEqual(client.work.call_count, 2)

    def test_conflicting_doi_does_not_match_export_by_key(self):
        citations = CitationGraph([{'ID': 'a', 'doi': '10.1234/first'}, {'ID': 'b'}])
        citations.add_citation_export([{'ID': 'a', 'doi': '10.1234/other', 'source': 'b'}],
                                      forward=True)
        self.assertEqual(citations.edges, set())


if __name__ == '__main__':
    unittest.main()
