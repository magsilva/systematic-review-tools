import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

import bibtexparser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from merge_bibtex import main, merge_entries, merge_files


def paper(key, **fields):
    return dict(ID=key, ENTRYTYPE='article', **fields)


class MergeBibtexTests(unittest.TestCase):
    def test_source_union_including_bridge_records(self):
        result = merge_entries([
            [paper('a', doi='10.1/a', source='scopus; Scopus')],
            [paper('b', scopus_eid='2-s2.0-1', source='ACM; ')],
            [paper('c', doi='10.1/a', scopus_eid='2-s2.0-1', source='acm')],
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['source'], 'ACM; Scopus')
        self.assertEqual(merge_entries([result, result])[0]['source'], 'ACM; Scopus')

    def test_missing_and_distinct_sources(self):
        result = merge_entries([[paper('a', doi='10.1/a'),
                                 paper('b', doi='10.1/a', source='Scopus'),
                                 paper('c', doi='10.1/c', source='ACM'),
                                 paper('d', doi='10.1/d')]])
        self.assertEqual([e.get('source') for e in result], ['Scopus', 'ACM', None])

    def test_unmatched_literal_braces_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'in.bib', Path(folder) / 'out.bib'
            source.write_text(r'@article{a, title={A study}, abstract={Java \{ host}}' '\n'
                              r'@article{b, title={Another study}, abstract={name\} suffix}}')
            self.assertEqual(merge_files([source], output), (2, 2))
            entries = bibtexparser.loads(output.read_text()).entries
            self.assertEqual(entries[0]['abstract'], r'Java \textbraceleft{} host')
            self.assertEqual(entries[1]['abstract'], r'name\textbraceright{} suffix')

    def test_doi_normalization_enrichment_and_conflicts(self):
        with self.assertLogs('merge_bibtex', level='WARNING'):
            result = merge_entries([
                [paper('first', doi='https://doi.org/10.1/A_B', title='First', abstract='')],
                [paper('second', doi=r'doi:10.1/a\_b', title='Second', abstract='Details')],
            ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], 'First')
        self.assertEqual(result[0]['abstract'], 'Details')

    def test_title_year_and_identifier_conflicts(self):
        with self.assertLogs('merge_bibtex', level='WARNING'):
            result = merge_entries([[paper('a', title='A {Study}', year='2026', doi='10.1/a'),
                                     paper('b', title='a study', year='2026', doi='10.1/b'),
                                     paper('c', title='A {Study}', year='2025')]])
        self.assertEqual(len(result), 3)
        result = merge_entries([[paper('a', title='A {Study}', year='2026'),
                                 paper('b', title='a study', year='2026', abstract='Text')]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['abstract'], 'Text')

    def test_bridge_and_eid(self):
        result = merge_entries([[paper('a', doi='10.1/a'), paper('b', scopus_eid='2-s2.0-1'),
                                 paper('c', doi='10.1/a', scopus_eid='2-s2.0-1')]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['ID'], 'a')

    def test_key_collision_and_crossref_remapping(self):
        result = merge_entries([
            [paper('parent', title='One'), paper('parent_2', title='Reserved')],
            [paper('parent', title='Two'), paper('child', crossref='parent')],
        ])
        self.assertEqual([e['ID'] for e in result], ['parent', 'parent_2', 'parent_3', 'child'])
        self.assertEqual(result[-1]['crossref'], 'parent_3')

    def test_duplicate_parent_alias(self):
        result = merge_entries([[paper('a', doi='10.1/a')],
                                [paper('b', doi='10.1/a'), paper('child', crossref='b')]])
        self.assertEqual(result[-1]['crossref'], 'a')

    def test_same_key_does_not_establish_identity(self):
        self.assertEqual(len(merge_entries([[paper('same', title='One')],
                                            [paper('same', title='Two')]])), 2)
        with self.assertRaisesRegex(ValueError, 'Ambiguous citation key'):
            merge_entries([[paper('same', title='One'), paper('same', title='Two')]])

    def test_files_round_trip_and_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second, output = [Path(folder) / name for name in ('a.bib', 'b.bib', 'out.bib')]
            first.write_text('@string{pub="ACM"}\n@article{a, title={Café}, doi={10.1/a}, publisher=pub, source={ACM}}', encoding='utf-8')
            second.write_text('@article{b, title={Café}, doi={10.1/a}, abstract={A study}, source={Scopus}}', encoding='utf-8')
            self.assertEqual(merge_files([first, second], output), (2, 1))
            database = bibtexparser.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(database.entries[0]['publisher'], 'ACM')
            self.assertEqual(database.entries[0]['abstract'], 'A study')
            self.assertEqual(database.entries[0]['source'], 'ACM; Scopus')
            previous = output.read_bytes()
            second.write_text('@article{broken, title={oops')
            with self.assertRaises(ValueError):
                merge_files([first, second], output)
            self.assertEqual(output.read_bytes(), previous)

    def test_cli_logs_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'errors.log'
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = main([str(Path(folder) / 'missing.bib'), '-o',
                               str(Path(folder) / 'out.bib'), '--log-file', str(log)])
            self.assertEqual(status, 1)
            self.assertIn('BibTeX merge failed', stderr.getvalue())
            self.assertIn('BibTeX merge failed', log.read_text())


if __name__ == '__main__':
    unittest.main()
