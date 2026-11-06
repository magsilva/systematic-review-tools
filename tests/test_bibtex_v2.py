"""Integration checks against the pinned v2 API and shared I/O boundary."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import bibtexparser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import new_database, parse_bibtex, read_bibtex, serialize_bibtex, write_bibtex
from bib2fastread import BibtexFilter as CsvFilter

spec = importlib.util.spec_from_file_location(
    'mapping_filter', Path(__file__).resolve().parents[1] / 'src/systematic-mapping-filter.py')
mapping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapping)


class BibtexV2Tests(unittest.TestCase):
    def test_pinned_version(self):
        self.assertEqual(bibtexparser.__version__, '2.1.0')

    def test_native_round_trip_preserves_metadata(self):
        original = dict(ID='paper', ENTRYTYPE='customtype', title='Café {Study}',
                        source='ACM; Scopus', file='pdfs/a.pdf', doi='10.1/a')
        database = new_database([original], ['A preamble'])
        content = serialize_bibtex(database)
        native = bibtexparser.parse_string(content)
        self.assertFalse(native.failed_blocks)
        self.assertEqual(dict(native.entries[0]), original)
        self.assertEqual(native.preambles[0].value, 'A preamble')
        self.assertEqual(database.entries[0], original)

    def test_macro_case_month_and_field_normalization(self):
        result = parse_bibtex('@string{pub="ACM"}\n@article{a, Title={Study}, publisher=PUB, month=jan}')
        self.assertEqual(result.entries[0]['title'], 'Study')
        self.assertEqual(result.entries[0]['publisher'], 'ACM')
        self.assertEqual(result.entries[0]['month'], 'January')

    def test_writer_failure_preserves_output_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'out.bib'
            output.write_text('previous output')
            def fail(stream, *args, **kwargs):
                stream.write('partial output')
                raise OSError('write failed')
            with patch('bibtexparser.write_file', side_effect=fail):
                with self.assertRaises(OSError):
                    write_bibtex(new_database(), output)
            self.assertEqual(output.read_text(), 'previous output')
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_csv_latin1_and_inherited_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'in.bib', Path(directory) / 'out.csv'
            source.write_bytes('@proceedings{p, title={Café}, year={2026}}\n'
                               '@inproceedings{c, title={A {Study}}, crossref={p}}'.encode('latin-1'))
            converter = CsvFilter()
            converter.set_input_file(source)
            converter.set_output_csv_file(output)
            converter.add_entry_type('inproceedings')
            converter.add_field('Title', ['plain_title'], '')
            converter.add_field('Year', ['year'], '')
            converter.run()
            self.assertIn('A Study,2026', output.read_text())

    def test_linter_changes_keys_and_references_only(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'in.bib', Path(directory) / 'out.bib'
            source.write_text('@proceedings{café, title={Parent}}\n'
                              '@article{child, title={Example @article{café,} text}, crossref={café}}')
            linter = mapping.BibtexLinter()
            linter.set_input_file(source)
            linter.set_output_file(output)
            linter.fix()
            records = read_bibtex(output).entries
            self.assertEqual(records[0]['ID'], 'cafe')
            self.assertEqual(records[1]['crossref'], 'cafe')
            self.assertEqual(records[1]['title'], 'Example @article{café,} text')

    def test_linter_rejects_key_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'in.bib', Path(directory) / 'out.bib'
            source.write_text('@article{café, title={One}}\n@article{cafe, title={Two}}')
            output.write_text('previous output')
            linter = mapping.BibtexLinter()
            linter.set_input_file(source)
            linter.set_output_file(output)
            with self.assertRaises(ValueError):
                linter.fix()
            self.assertEqual(output.read_text(), 'previous output')

    def test_empty_mapping_filter_writes_empty_bibliography(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'in.bib', Path(directory) / 'out.bib'
            source.write_text('')
            output.write_text('previous output')
            filtering = mapping.BibtexFilter()
            filtering.set_input_file(source)
            filtering.set_output_bibtex_file(output)
            filtering.set_output_csv_file(Path(directory) / 'out.csv')
            filtering.apply_filters()
            self.assertEqual(read_bibtex(output).entries, [])
