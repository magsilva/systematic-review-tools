import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import main as application


class ApplicationTests(unittest.TestCase):
    def test_existing_input_type_pico_and_download_pipeline(self):
        from bibtex_io import read_bibtex
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = [Path(folder) / name for name in ('in.bib', 'out.bib', 'log')]
            source.write_text('@article{a, title={Autism software engineering}, doi={10.1/a}}\n'
                              '@book{b, title={Autism software engineering}}\n'
                              '@article{c, title={Other topic}}')
            original = source.read_bytes()
            def download(options):
                records = read_bibtex(options[0]).entries
                self.assertEqual([entry['ID'] for entry in records], ['a'])
                self.assertIn('--exclude-source', options)
                self.assertIn('--rate-limit', options)
                output.write_text('mock download output')
                return 1  # A downloader may save progress while reporting failures.
            with patch('download_pdfs.main', side_effect=download), \
                    patch('scopus_search.main') as search, contextlib.redirect_stdout(io.StringIO()):
                status = application.main(['--input', str(source), '--output', str(output),
                    '--log-file', str(log), '--include-type', 'article', 'inproceedings',
                    'incollection', '--filter-pico', '--population', 'autism',
                    '--intervention', 'software engineering', '--download-pdfs',
                    '--exclude-source', 'ACM', '--rate-limit', '3'])
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(), 'mock download output')
            self.assertEqual(source.read_bytes(), original)
            search.assert_not_called()

    def test_search_then_filter(self):
        from bibtex_io import read_bibtex
        with tempfile.TemporaryDirectory() as folder:
            output, log = Path(folder) / 'out.bib', Path(folder) / 'log'
            def search(args, keywords):
                args.output.write_text('@article{a, title={Autism software engineering}}\n'
                                       '@book{b, title={Autism software engineering}}')
                return 0
            with patch.dict(application.SEARCH_ENGINES, scopus=search), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = application.main(['--engine', 'scopus', '--population', 'autism',
                    '--intervention', 'software engineering', '--filter-pico',
                    '--include-type', 'article', '--output', str(output), '--log-file', str(log)])
            self.assertEqual(status, 0)
            self.assertEqual([e['ID'] for e in read_bibtex(output).entries], ['a'])

    def test_failed_search_never_filters_or_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(application.SEARCH_ENGINES, scopus=lambda *args: 1), \
                    patch('filter_bibtex.main') as filtering, patch('download_pdfs.main') as downloading:
                status = application.main(['--engine', 'scopus', '--population', 'autism',
                    '--download-pdfs', '--output', str(Path(folder) / 'out.bib')])
            self.assertEqual(status, 1)
            filtering.assert_not_called()
            downloading.assert_not_called()

    def test_pending_acm_import_does_not_process_old_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out.bib'
            output.write_text('previous output')
            with patch.dict(application.SEARCH_ENGINES, acm=lambda *args: 0), \
                    patch('download_pdfs.main') as downloading, contextlib.redirect_stdout(io.StringIO()):
                status = application.main(['--engine', 'acm', '--population', 'autism',
                    '--download-pdfs', '--output', str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(output.read_text(), 'previous output')
            downloading.assert_not_called()

    def test_input_protection_and_empty_pico_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'in.bib'
            source.write_text('@article{a, title={Autism}}')
            for options in (['--output', str(source)],
                            ['--output', str(Path(folder) / 'out.bib'), '--filter-pico']):
                with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        application.main(['--input', str(source)] + options)
            self.assertIn('Autism', source.read_text())

    def test_keyword_to_search_integration(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'papers.bib'
            log = Path(folder) / 'errors.log'
            with patch('scopus_search.search_to_bibtex', return_value=3) as search, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = application.main([
                    '--engine', 'Scopus', '--population', 'adults',
                    '--population', 'children', '--intervention', 'exercise',
                    '--control', 'usual care', '--outcome', 'quality of life',
                    '--output', str(output), '--log-file', str(log),
                    '--config-path', 'custom.cfg', '--refresh', '--non-subscriber'])
            self.assertEqual(status, 0)
            search.assert_called_once_with(
                'TITLE-ABS-KEY("adults" OR "children") AND TITLE-ABS-KEY("exercise") '
                'AND TITLE-ABS-KEY("usual care") AND TITLE-ABS-KEY("quality of life")',
                output, refresh=True, subscriber=False, config_path=Path('custom.cfg'))
            self.assertIn('Saved 3 records', stdout.getvalue())

    def test_search_failure_reaches_log_and_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'errors.log'
            with patch('scopus_search.search_to_bibtex', side_effect=RuntimeError('API failed')), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = application.main([
                    '--engine', 'scopus', '--population', 'adults',
                    '--output', str(Path(folder) / 'papers.bib'), '--log-file', str(log)])
            self.assertEqual(status, 1)
            self.assertIn('API failed', stderr.getvalue())
            self.assertIn('API failed', log.read_text())

    def test_invalid_inputs_never_search(self):
        for options in (['--engine', 'unknown', '--population', 'adults'],
                        ['--engine', 'scopus'],
                        ['--engine', 'scopus', '--population', ' ']):
            with self.subTest(options=options), patch('scopus_search.main') as runner, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    application.main(options + ['--output', 'papers.bib'])
                self.assertEqual(error.exception.code, 2)
                runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
