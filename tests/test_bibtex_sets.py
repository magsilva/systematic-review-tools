import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import read_bibtex
from bibtex_sets import main, same_publication, set_entries, set_files


def paper(key, **fields):
    return dict(ID=key, ENTRYTYPE='article', **fields)


class BibtexSetTests(unittest.TestCase):
    def test_identical_anonymous_records_with_different_keys_and_sources(self):
        a = dict(ID='3643142', ENTRYTYPE='proceedings', title='Conference',
                 year='2024', publisher='ACM', source='ACM')
        b = dict(a, ID='another-key', source='Scopus', file='local.pdf')
        self.assertTrue(same_publication(a, b))
        for operation, count in [('union', 1), ('intersection', 1),
                                 ('difference', 0), ('symmetric-difference', 0)]:
            with self.subTest(operation=operation):
                self.assertEqual(len(set_entries([[a], [b]], operation)), count)
        self.assertEqual(set_entries([[a], [b]], 'union')[0]['source'], 'ACM; Scopus')
        self.assertFalse(same_publication(a, dict(b, year='2025')))
        self.assertFalse(same_publication(paper('a', title='Same'), paper('b', title='Same')))

    def test_editor_fallback_and_doi_url(self):
        a = dict(ID='a', ENTRYTYPE='book', title='Collected {Studies}', editor='Jane Doe', year='2024')
        b = dict(a, ID='b', publisher='ACM')
        self.assertTrue(same_publication(a, b))
        self.assertEqual(len(set_entries([[a], [b]], 'union')), 1)
        self.assertFalse(same_publication(a, dict(b, editor='John Doe')))
        self.assertFalse(same_publication(a, dict(b, editor='', author='Jane Doe')))
        self.assertFalse(same_publication(dict(a, abstract='One'), dict(b, abstract='Two')))
        self.assertFalse(same_publication(dict(a, doi='10.1/a'), dict(b, doi='10.1/b')))
        self.assertTrue(same_publication(dict(a, url='https://doi.org/10.1/A'),
                                         paper('different', doi='10.1/a')))

    def test_percent_does_not_hide_abstract_difference(self):
        a = paper('a', title='Study', author='Author', abstract='50% about one topic')
        b = dict(a, ID='b', abstract='50% about another topic')
        self.assertFalse(same_publication(a, b))

    def test_all_operations_and_duplicate_membership(self):
        a, b, c = [paper(key, doi='10.1/' + key) for key in 'abc']
        sources = [[a, a, b], [b, c]]
        for operation, expected in [('union', 'abc'), ('merge', 'abc'),
                                    ('intersection', 'b'), ('difference', 'a'),
                                    ('symmetric-difference', 'ac')]:
            with self.subTest(operation=operation):
                self.assertEqual([e['ID'] for e in set_entries(sources, operation)], list(expected))

    def test_normalized_doi_wins_over_metadata(self):
        self.assertTrue(same_publication(paper('a', doi='https://doi.org/10.1/ABC', title='One'),
                                         paper('b', doi='doi:10.1/abc', title='Two')))
        self.assertFalse(same_publication(paper('a', doi='10.1/a', title='Same', author='A'),
                                          paper('b', doi='10.1/b', title='Same', author='A')))

    def test_fallback_uses_title_authors_and_available_abstract(self):
        a = paper('a', title='A {Study}', author='Jane Doe and John Smith', abstract='Text')
        b = paper('b', title='a study', author='Jane Doe and John Smith')
        self.assertTrue(same_publication(a, b))
        self.assertTrue(same_publication(dict(a, doi='10.1/a'), b))
        self.assertFalse(same_publication(a, dict(b, abstract='Different')))
        self.assertFalse(same_publication(a, dict(b, author='Another Author')))
        self.assertFalse(same_publication(a, paper('a', title='A Study')))
        self.assertFalse(same_publication(paper('a'), paper('a')))

    def test_bridge_does_not_merge_conflicting_dois_or_abstracts(self):
        common = dict(title='Study', author='Author')
        sources = [[paper('a', doi='10.1/a', **common), paper('b', doi='10.1/b', **common)],
                   [paper('c', **common)]]
        result = set_entries(sources, 'union')
        self.assertEqual(len(result), 2)
        sources = [[paper('a', abstract='One', **common)], [paper('b', **common)],
                   [paper('c', abstract='Two', **common)]]
        self.assertEqual(len(set_entries(sources, 'union')), 2)

    def test_metadata_sources_key_collisions_and_crossrefs(self):
        sources = [[paper('a', doi='10.1/a', source='ACM'), paper('a_2', doi='10.1/z')],
                   [paper('a', doi='10.1/b'), paper('child', crossref='a'),
                    paper('alias', doi='10.1/a', source='scopus', abstract='Details')]]
        result = set_entries(sources, 'union')
        self.assertEqual([e['ID'] for e in result], ['a', 'a_2', 'a_3', 'child'])
        self.assertEqual(result[-1]['crossref'], 'a_3')
        self.assertEqual(result[0]['source'], 'ACM; Scopus')
        self.assertEqual(result[0]['abstract'], 'Details')
        self.assertNotIn('abstract', sources[0][0])

    def test_multiple_inputs_odd_membership_and_difference(self):
        a, b, c = [paper(key, doi='10.1/' + key) for key in 'abc']
        sources = [[a, b, c], [a, b], [a]]
        self.assertEqual([e['ID'] for e in set_entries(sources, 'symmetric-difference')], ['a', 'c'])
        self.assertEqual([e['ID'] for e in set_entries(sources, 'difference')], ['c'])
        self.assertEqual([e['ID'] for e in set_entries(sources, 'intersection')], ['a'])

    def test_files_empty_output_and_parse_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b, output = [Path(folder)/name for name in ('a.bib', 'b.bib', 'out.bib')]
            a.write_text('@article{a, doi={10.1/a}}')
            b.write_text('@article{b, doi={10.1/b}}')
            self.assertEqual(set_files([a, b], output, 'intersection'), (2, 0))
            self.assertEqual(read_bibtex(output).entries, [])
            output.write_text('Previous output')
            b.write_text('\n@article{bad, title={oops')
            with self.assertRaisesRegex(ValueError, 'line 2'):
                set_files([a, b], output, 'union')
            self.assertEqual(output.read_text(), 'Previous output')
            with self.assertRaises(ValueError):
                set_files([a, b], a, 'union')

    def test_cli_and_error_logging(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b, output, log = [Path(folder)/name for name in ('a.bib', 'b.bib', 'out.bib', 'log')]
            a.write_text('@article{a, doi={10.1/a}}')
            b.write_text('@article{b, doi={10.1/a}}')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['intersection', str(a), str(b), '-o', str(output),
                                       '--log-file', str(log)]), 0)
            self.assertEqual(len(read_bibtex(output).entries), 1)
            b.write_text('@article{bad, title={oops')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(['union', str(a), str(b), '-o', str(output),
                                       '--log-file', str(log)]), 1)
            self.assertIn('line 1', stderr.getvalue())
            self.assertIn('line 1', log.read_text())
