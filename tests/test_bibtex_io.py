import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import BibtexParseError, parse_bibtex
from merge_bibtex import main as merge_main, merge_files
from acm_search import import_bibtex


class BibtexDiagnosticsTests(unittest.TestCase):
    def test_malformed_record_reports_starting_line(self):
        content = '\n@article{good, title={Good}}\n\n\n@article{bad,\n title={Unclosed\n'
        with self.assertRaises(BibtexParseError) as caught:
            parse_bibtex(content, 'papers.bib')
        self.assertEqual(caught.exception.line, 5)
        self.assertIn('papers.bib: line 5:', str(caught.exception))

    def test_undefined_macro_reports_entry_line(self):
        with self.assertRaises(BibtexParseError) as caught:
            parse_bibtex('\n\n@article{bad, title=undefinedmacro}\n', 'macro.bib')
        self.assertEqual(caught.exception.line, 3)
        self.assertIn('undefinedmacro', str(caught.exception))

    def test_comments_and_unparsed_text(self):
        for block in ('@comment{A comment}', 'not bibtex'):
            with self.subTest(block=block), self.assertRaises(BibtexParseError) as caught:
                parse_bibtex('\n\n' + block, 'input.bib')
            self.assertEqual(caught.exception.line, 3)

    def test_missing_title_reports_entry_line(self):
        with self.assertRaises(BibtexParseError) as caught:
            parse_bibtex('@article{a, title={Good}}\n\n@article{b, year={2026}}',
                         'export.bib', require_titles=True)
        self.assertEqual(caught.exception.line, 3)
        self.assertIn('missing a title', str(caught.exception))

    def test_line_numbers_survive_literal_brace_normalization(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.bib'
            source.write_text('@article{a, title={Literal \\{}}\n\n@article{broken, title={oops')
            with self.assertRaises(BibtexParseError) as caught:
                merge_files([source], Path(folder) / 'out.bib')
            self.assertEqual(caught.exception.line, 3)

    def test_import_reports_source_line_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'export.bib', Path(folder) / 'out.bib'
            source.write_text('\n\n@article{bad, title={oops')
            output.write_text('old output')
            with self.assertRaisesRegex(BibtexParseError, 'export.bib: line 3:'):
                import_bibtex(source, output)
            self.assertEqual(output.read_text(), 'old output')

    def test_merge_reports_line_in_stderr_and_log(self):
        with tempfile.TemporaryDirectory() as folder:
            source, log = Path(folder) / 'bad.bib', Path(folder) / 'errors.log'
            source.write_text('\n@article{bad, title={oops')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = merge_main([str(source), '-o', str(Path(folder) / 'out.bib'),
                                     '--log-file', str(log)])
            self.assertEqual(status, 1)
            expected = '%s: line 2:' % source
            self.assertIn(expected, stderr.getvalue())
            self.assertIn(expected, log.read_text())


if __name__ == '__main__':
    unittest.main()
