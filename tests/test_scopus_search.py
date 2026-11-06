import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import scopus_search


class ScopusSearchTests(unittest.TestCase):
    def test_metadata_conversion(self):
        record = SimpleNamespace(
            eid='2-s2.0-123', subtype='cp', title='A {study} & 50%',
            author_names='Silva, Maria;Smith, John', coverDate='2025-01-01',
            publicationName='Proceedings', pageRange='10-20', doi='10.1/test',
            description='An abstract',
        )
        result = scopus_search.record_to_bibtex(record, 1)
        for expected in ('@inproceedings{scopus_2-s2_0-123_1,',
                         'author = {Silva, Maria and Smith, John}',
                         r'title = {A \textbraceleft{}study\textbraceright{} \& 50\%}',
                         'year = {2025}', 'booktitle = {Proceedings}',
                         'pages = {10--20}', 'abstract = {An abstract}'):
            self.assertIn(expected, result)

    def test_missing_metadata(self):
        self.assertEqual(scopus_search.record_to_bibtex(SimpleNamespace(), 1),
                         '@misc{scopus_result_1,\n\n}\n')

    def test_retrieval_and_empty_results(self):
        package = Mock()
        search_class = Mock(return_value=SimpleNamespace(results=[
            SimpleNamespace(subtype='ar', title='Paper')]))
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {
            'pybliometrics': package,
            'pybliometrics.scopus': SimpleNamespace(ScopusSearch=search_class),
        }):
            output = Path(folder) / 'papers.bib'
            self.assertEqual(scopus_search.search_to_bibtex('TITLE(test)', output), 1)
            self.assertIn('title = {Paper}', output.read_text())
            package.init.assert_called_once_with()
            search_class.assert_called_once_with(
                'TITLE(test)', refresh=False, subscriber=True, download=True, verbose=False)
            search_class.return_value.results = None
            self.assertEqual(scopus_search.search_to_bibtex('TITLE(test)', output), 0)
            self.assertEqual(output.read_text(), '')

    def test_conversion_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {
            'pybliometrics': Mock(),
            'pybliometrics.scopus': SimpleNamespace(ScopusSearch=Mock(
                return_value=SimpleNamespace(results=[SimpleNamespace(author_names=123)]))),
        }):
            output = Path(folder) / 'papers.bib'
            output.write_text('previous export')
            with self.assertRaises(AttributeError):
                scopus_search.search_to_bibtex('test', output)
            self.assertEqual(output.read_text(), 'previous export')
            self.assertEqual(list(Path(folder).iterdir()), [output])

    def test_errors_logged_to_file_and_stderr(self):
        for error in (RuntimeError('API failure'), OSError('disk failure')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / 'errors.log'
                stderr = io.StringIO()
                with patch.object(scopus_search, 'search_to_bibtex', side_effect=error), \
                        contextlib.redirect_stderr(stderr):
                    status = scopus_search.main([
                        '--query', 'test', '--output', str(Path(folder) / 'out.bib'),
                        '--log-file', str(log)])
                self.assertEqual(status, 1)
                self.assertIn(str(error), stderr.getvalue())
                self.assertIn(str(error), log.read_text())


if __name__ == '__main__':
    unittest.main()
