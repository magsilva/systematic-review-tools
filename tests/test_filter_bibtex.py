import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import parse_bibtex, new_database, serialize_bibtex
from filter_bibtex import Criterion, EntryTypeCriterion, PicoCriterion, filter_entries, filter_file, main


ENTRIES = [
    {'ID': 'a', 'ENTRYTYPE': 'article', 'title': 'Article', 'year': '2026'},
    {'ID': 'b', 'ENTRYTYPE': 'book', 'title': 'Book'},
    {'ID': 'c', 'ENTRYTYPE': 'inproceedings', 'title': 'Conference paper'},
    {'ID': 'd', 'ENTRYTYPE': 'misc', 'title': 'Other'},
]


class FilterBibtexTests(unittest.TestCase):
    def test_pico_requires_every_supplied_component_across_fields(self):
        criterion = PicoCriterion(population=['autism', 'ADHD'],
                                  intervention=['software engineering'],
                                  control=['usual care'], outcome=['productivity'])
        entry = {'title': 'ADHD in SOFTWARE ENGINEERING',
                 'abstract': 'Compared with usual care', 'keywords': 'productivity'}
        self.assertTrue(criterion.accepts(entry))
        for field in ('title', 'abstract', 'keywords'):
            with self.subTest(field=field):
                self.assertFalse(criterion.accepts({k: v for k, v in entry.items() if k != field}))

    def test_pico_empty_components_missing_fields_and_literal_matching(self):
        self.assertTrue(PicoCriterion().accepts({}))
        self.assertTrue(PicoCriterion(population=[' ', 'Autism']).accepts({'abstract': 'autism'}))
        self.assertFalse(PicoCriterion(population=['autism']).accepts({}))
        self.assertFalse(PicoCriterion(population=['autism']).accepts({'author': 'autism'}))
        self.assertFalse(PicoCriterion(intervention=['software engineering']).accepts(
            {'title': 'software', 'abstract': 'engineering'}))
        self.assertTrue(PicoCriterion(population=['auti']).accepts({'title': 'autistic'}))
        self.assertFalse(PicoCriterion(population=['a.b']).accepts({'title': 'axb'}))

    def test_pico_decodes_tex_without_modifying_entry(self):
        entry = {'title': r'A {Software} Engineering study of neurodiversit\'{e}'}
        original = dict(entry)
        self.assertTrue(PicoCriterion(population=['neurodiversité'],
                                      intervention=['software engineering']).accepts(entry))
        self.assertEqual(entry, original)

    def test_cli_pico_combines_with_type_filter_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = (Path(folder)/p for p in ('in.bib', 'out.bib', 'log'))
            source.write_text('@article{a, title={Autism}, abstract={Software engineering}, source={ACM}}\n'
                              '@book{b, title={Autism software engineering}}\n'
                              '@article{c, title={Autism}}\n'
                              '@article{d, title={ADHD}, keywords={Software engineering}}')
            original = source.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                status = main([str(source), '-o', str(output), '--log-file', str(log),
                               '--include-type', 'article', '--population', 'autism',
                               '--population', 'ADHD', '--intervention', 'software engineering'])
            self.assertEqual(status, 0)
            entries = parse_bibtex(output.read_text()).entries
            self.assertEqual([entry['ID'] for entry in entries], ['a', 'd'])
            self.assertEqual(entries[0]['source'], 'ACM')
            self.assertEqual(source.read_bytes(), original)

    def test_include_multiple_types_and_exclusion_priority(self):
        included = filter_entries(ENTRIES, [EntryTypeCriterion(['ARTICLE', 'inproceedings'])])
        self.assertEqual([entry['ID'] for entry in included], ['a', 'c'])
        kept = filter_entries(ENTRIES, [EntryTypeCriterion(['article', 'inproceedings'], ['article'])])
        self.assertEqual([entry['ID'] for entry in kept], ['c'])

    def test_exclude_only_no_criteria_and_empty_result(self):
        self.assertEqual([e['ID'] for e in filter_entries(ENTRIES, [EntryTypeCriterion(exclude_types=['book'])])],
                         ['a', 'c', 'd'])
        self.assertEqual(filter_entries(ENTRIES, []), ENTRIES)
        self.assertEqual(filter_entries(ENTRIES, [EntryTypeCriterion(['phdthesis'])]), [])

    def test_multiple_criteria_are_conjunctive_and_do_not_mutate_input(self):
        class HasYear(Criterion):
            def accepts(self, entry):
                return 'year' in entry
        kept = filter_entries(ENTRIES, [EntryTypeCriterion(['article', 'inproceedings']), HasYear()])
        self.assertEqual([e['ID'] for e in kept], ['a'])
        kept[0]['title'] = 'Changed'
        self.assertEqual(ENTRIES[0]['title'], 'Article')

    def test_cli_preserves_fields_unicode_macros_and_order(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = (Path(folder)/p for p in ('in.bib', 'out.bib', 'log'))
            source.write_text('@string{pub="ACM"}\n'
                              '@article{a, title={Café}, publisher=pub, source={ACM; Scopus}, file={a.pdf}}\n'
                              '@book{b, title={Book}}\n@inproceedings{c, title={Paper}}\n')
            original = source.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = main([str(source), '-o', str(output), '--log-file', str(log),
                               '--include-type', 'Article', '--include-type', 'inproceedings',
                               '--exclude-type', 'book'])
            self.assertEqual(status, 0)
            entries = parse_bibtex(output.read_text()).entries
            self.assertEqual([e['ID'] for e in entries], ['a', 'c'])
            self.assertEqual(entries[0]['title'], 'Café')
            self.assertEqual(entries[0]['publisher'], 'ACM')
            self.assertEqual(entries[0]['source'], 'ACM; Scopus')
            self.assertEqual(entries[0]['file'], 'a.pdf')
            self.assertEqual(source.read_bytes(), original)
            self.assertIn('kept 2; excluded 1', stdout.getvalue())

    def test_parse_failure_has_line_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = (Path(folder)/p for p in ('in.bib', 'out.bib', 'log'))
            source.write_text('\n@article{bad, title={oops')
            output.write_text('Previous result')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = main([str(source), '-o', str(output), '--log-file', str(log)])
            self.assertEqual(status, 1)
            self.assertIn('in.bib: line 2:', stderr.getvalue())
            self.assertIn('in.bib: line 2:', log.read_text())
            self.assertEqual(output.read_text(), 'Previous result')

    def test_rejects_input_overwrite_and_saves_empty_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder)/'in.bib', Path(folder)/'out.bib'
            source.write_text('@book{b, title={Book}}')
            with self.assertRaisesRegex(ValueError, 'different file'):
                filter_file(source, source, [])
            self.assertEqual(filter_file(source, output, [EntryTypeCriterion(['article'])]), (1, 0))
            self.assertEqual(parse_bibtex(output.read_text()).entries, [])


if __name__ == '__main__':
    unittest.main()
