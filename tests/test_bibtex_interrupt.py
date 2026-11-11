import contextlib
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))

import bibtex_sets
import extract_citing_papers
import extract_references
import filter_bibtex
import main as application
import merge_bibtex
import scopus_search
import screen_studies
import sol_search
from bibtex_io import read_bibtex


class InterruptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / 'in.bib'
        self.source.write_text('@article{first, title={First paper}}\n'
                               '@article{second, title={Second paper}}\n')
        self.output = self.directory / 'out.bib'
        self.output.write_text('@article{old, title={Previous output}}')
        self.checkpoint = self.directory / 'out-incomplete.bib'
        self.log = self.directory / 'run.log'
        redirect = contextlib.redirect_stderr(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def assert_checkpoint(self, expected_keys):
        self.assertEqual([entry['ID'] for entry in read_bibtex(self.output).entries], ['old'])
        records = read_bibtex(self.checkpoint).entries
        self.assertEqual([entry['ID'] for entry in records], expected_keys)
        return records

    def test_real_signals_save_checkpoint_and_exit_with_signal_status(self):
        command = """
import os
import sys
import filter_bibtex

def accepts(self, entry):
    if entry['ID'] == 'second':
        os.kill(os.getpid(), int(sys.argv[1]))
    return True

filter_bibtex.EntryTypeCriterion.accepts = accepts
sys.exit(filter_bibtex.main(sys.argv[2:]))
"""
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT'):
            if not hasattr(signal, name):
                continue
            signum = getattr(signal, name)
            with self.subTest(signal=name):
                result = subprocess.run(
                    [sys.executable, '-c', command, str(signum), str(self.source),
                     '-o', str(self.output), '--log-file', str(self.log)],
                    cwd=SRC, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 128 + signum, result.stderr)
                self.assertIn('saved 1 processed entries', result.stderr)
                self.assert_checkpoint(['first'])
                self.checkpoint.unlink()

    def test_interrupt_does_not_replace_existing_checkpoint_or_input(self):
        self.checkpoint.write_text('@article{existing, title={Older checkpoint}}')
        with patch.object(filter_bibtex.EntryTypeCriterion, 'accepts', side_effect=[True, KeyboardInterrupt]):
            self.assertEqual(filter_bibtex.main([
                str(self.source), '-o', str(self.output), '--log-file', str(self.log)]), 130)
        self.assert_checkpoint(['existing'])
        path = self.directory / 'out-2-incomplete.bib'
        self.assertEqual([entry['ID'] for entry in read_bibtex(path).entries], ['first'])
        self.assertEqual(len(read_bibtex(self.source).entries), 2)

    def test_interrupt_in_pipeline_saves_next_to_final_output(self):
        with patch.object(filter_bibtex.EntryTypeCriterion, 'accepts', side_effect=[True, KeyboardInterrupt]):
            self.assertEqual(application.main([
                '--input', str(self.source), '--output', str(self.output),
                '--log-file', str(self.log)]), 130)
        self.assert_checkpoint(['first'])
        self.assertFalse(any(path.is_dir() for path in self.directory.iterdir()))

    def test_pdf_checkpoint_only_includes_finished_jobs(self):
        self.source.write_text('@article{first, source={ACM}, doi={10.1145/a}}\n'
                               '@article{second, source={ACM}, doi={10.1145/b}}\n')
        pdfs = self.directory / 'pdfs'
        pdf = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n'
        with patch('browser_pdf.BrowserPdfDownloader') as factory, contextlib.redirect_stdout(io.StringIO()):
            browser = factory.return_value.__enter__.return_value
            browser.download.side_effect = [pdf, KeyboardInterrupt]
            status = application.main([
                '--input', str(self.source), '--output', str(self.output), '--log-file', str(self.log),
                '--download-pdfs', '--pdf-dir', str(pdfs), '--rate-limit', '1000000000', '--no-progress'])
        self.assertEqual(status, 130)
        records = self.assert_checkpoint(['first'])
        self.assertEqual(Path(records[0]['file']).read_bytes(), pdf)
        self.assertEqual(len(list(pdfs.iterdir())), 1)

    def test_screening_checkpoint_only_includes_evaluated_studies(self):
        criteria = self.directory / 'rules.txt'
        criteria.write_text('I1 Relevant to the review\n')
        ratings = [{'id': 'I1', 'score': 6, 'assessable': True, 'reason': 'Relevant'}]
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key', 'OPENAI_MODEL': 'test-model'}), \
                patch('screen_studies.evaluate', side_effect=[ratings, KeyboardInterrupt]):
            status = screen_studies.main([str(self.source), '--criteria', str(criteria),
                                          '--output', str(self.output)])
        self.assertEqual(status, 130)
        self.assertEqual(self.assert_checkpoint(['first'])[0]['screening_decision'], 'selected')

    def test_merge_and_set_operation_keep_processed_records(self):
        other = self.directory / 'other.bib'
        other.write_text('@article{third, title={Third paper}}')
        operations = ((merge_bibtex.main, [], 'merge_bibtex.identity_keys'),
                      (bibtex_sets.main, ['union'], 'bibtex_sets.publication_identity'))
        for command, options, target in operations:
            with self.subTest(target=target):
                module = sys.modules[target.split('.')[0]]
                identify = getattr(module, target.split('.')[1])

                def interrupt_second(entry):
                    if entry['ID'] == 'second':
                        raise KeyboardInterrupt
                    return identify(entry)

                with patch(target, side_effect=interrupt_second):
                    status = command([*options, str(self.source), str(other),
                                      '-o', str(self.output), '--log-file', str(self.log)])
                self.assertEqual(status, 130)
                self.assert_checkpoint(['first'])
                self.checkpoint.unlink()

    def test_forward_citations_save_records_from_partially_retrieved_seed(self):
        def citing(_):
            yield {'id': 'https://openalex.org/W2', 'display_name': 'Citing paper'}
            raise KeyboardInterrupt

        with patch('extract_citing_papers.OpenAlex') as api, \
                patch('extract_citing_papers.configure_session'):
            api.return_value.resolve.return_value = 'W1'
            api.return_value.citing.side_effect = citing
            status = extract_citing_papers.main([
                str(self.source), '-o', str(self.output), '--log-file', str(self.log)])
        self.assertEqual(status, 130)
        self.assertEqual(self.assert_checkpoint(['citing_1'])[0]['source'], 'first')

    def test_references_save_completed_citations(self):
        self.source.write_text('@article{first, doi={10.1234/seed}}')
        with patch('extract_references.Crossref') as api:
            api.return_value.work.side_effect = [
                {'reference': [{'DOI': '10.1234/one'}, {'DOI': '10.1234/two'}]},
                {'DOI': '10.1234/one', 'title': ['A reference']},
                KeyboardInterrupt,
            ]
            status = extract_references.main([
                str(self.source), '-o', str(self.output), '--log-file', str(self.log),
                '--no-openalex', '--no-scholar'])
        self.assertEqual(status, 130)
        self.assertEqual(self.assert_checkpoint(['reference_1'])[0]['source'], '10.1234/seed')

    def test_scopus_saves_already_converted_records(self):
        def results():
            yield SimpleNamespace(subtype='ar', title='Search result')
            raise KeyboardInterrupt

        search = Mock(return_value=SimpleNamespace(results=results()))
        with patch.dict(sys.modules, {'pybliometrics': Mock(),
                                     'pybliometrics.scopus': SimpleNamespace(ScopusSearch=search)}):
            status = scopus_search.main(['--query', 'test', '--output', str(self.output),
                                         '--log-file', str(self.log)])
        self.assertEqual(status, 130)
        self.assertEqual(self.assert_checkpoint(['scopus_result_1'])[0]['source'], 'Scopus')

    def test_sol_saves_extracted_records_and_closes_browser(self):
        context = Mock()
        with patch('playwright.sync_api.sync_playwright'), \
                patch('sol_search.launch_context', return_value=context), \
                patch('sol_search.collect_result_urls', return_value=['one', 'two']), \
                patch('sol_search.extract_entry', side_effect=[
                    {'ID': 'one', 'ENTRYTYPE': 'article', 'title': 'Search result', 'source': 'SOL'},
                    KeyboardInterrupt]):
            status = sol_search.main(['--query', 'test', '--output', str(self.output),
                                      '--log-file', str(self.log)])
        self.assertEqual(status, 130)
        self.assertEqual(self.assert_checkpoint(['one'])[0]['source'], 'SOL')
        context.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
