import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import bibtexparser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import acm_search
import main as application


class AcmSearchTests(unittest.TestCase):
    def test_url_preserves_canonical_expression(self):
        query = '("software engineering" OR "C++ & tools") AND ("autism")'
        url = acm_search.build_search_url(query)
        self.assertEqual(urlparse(url).netloc, 'dl.acm.org')
        self.assertEqual(parse_qs(urlparse(url).query), {'AllField': [query]})

    def test_console_prepares_without_claiming_import(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('acm_search.sys.stdin.isatty', return_value=False), \
                patch('acm_search.webbrowser.open', return_value=True) as browser, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            output = Path(folder) / 'papers.bib'
            status = application.main([
                '--engine', 'acm', '--population', 'software engineering',
                '--intervention', 'autism', '--output', str(output),
                '--log-file', str(Path(folder) / 'errors.log')])
            self.assertEqual(status, 0)
            self.assertFalse(output.exists())
            self.assertIn('no papers imported', stdout.getvalue())
            query = parse_qs(urlparse(browser.call_args.args[0]).query)['AllField'][0]
            self.assertEqual(query, '("software engineering") AND ("autism")')

    def test_import_preserves_unicode_and_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'download.bib'
            output = Path(folder) / 'papers.bib'
            content = '@article{a, title={A {Study}}, author={Silva, José}, year={2026}}\n'
            source.write_text(content, encoding='utf-8')
            self.assertEqual(acm_search.import_bibtex(source, output), 1)
            original = bibtexparser.loads(content).entries[0]
            imported = bibtexparser.loads(output.read_text(encoding='utf-8')).entries[0]
            self.assertEqual(imported, dict(original, source='ACM'))
            self.assertEqual(source.read_text(encoding='utf-8'), content)

    def test_source_union_and_reimport_are_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'export.bib'
            source.write_text('@article{a, title={Test}, source={scopus; ACM; acm}}')
            self.assertEqual(acm_search.import_bibtex(source, source), 1)
            imported = bibtexparser.loads(source.read_text()).entries[0]
            self.assertEqual(imported['source'], 'ACM; Scopus')
            first_export = source.read_bytes()
            acm_search.import_bibtex(source, source)
            self.assertEqual(source.read_bytes(), first_export)

    def test_invalid_import_is_logged_and_preserves_output(self):
        for content in ('<html>Access denied</html>', '',
                        '@article{a, title={Good}}\n@article{broken, title={oops'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'download.bib'
                source.write_text(content)
                output = Path(folder) / 'papers.bib'
                output.write_text('previous output')
                log = Path(folder) / 'errors.log'
                with contextlib.redirect_stderr(io.StringIO()) as stderr, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        patch('acm_search.webbrowser.open') as browser:
                    status = acm_search.main([
                        '--query', '("autism")', '--output', str(output),
                        '--import-bibtex', str(source), '--log-file', str(log)])
                self.assertEqual(status, 1)
                self.assertIn('ACM browser search/import failed', stderr.getvalue())
                self.assertIn('ACM browser search/import failed', log.read_text())
                self.assertEqual(output.read_text(), 'previous output')
                browser.assert_not_called()

    def test_later_import_through_application(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch('acm_search.webbrowser.open') as browser, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            source = Path(folder) / 'export.bib'
            source.write_text('@inproceedings{a, title={Test}}')
            output = Path(folder) / 'papers.bib'
            status = application.main([
                '--engine', 'acm', '--population', 'autism', '--output', str(output),
                '--import-bibtex', str(source), '--log-file', str(Path(folder) / 'log')])
            self.assertEqual(status, 0)
            self.assertIn('Imported 1 records', stdout.getvalue())
            self.assertEqual(bibtexparser.loads(output.read_text()).entries[0]['source'], 'ACM')
            self.assertEqual(source.read_text(), '@inproceedings{a, title={Test}}')
            browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
