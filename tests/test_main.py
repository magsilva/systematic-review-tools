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
