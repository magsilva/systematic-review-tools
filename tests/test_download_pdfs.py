from collections import deque
import contextlib
import io
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import bibtexparser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from download_pdfs import (DownloadJob, HostRateLimiter, candidate_urls, download_bibtex,
                           download_queue, main, pdf_filename)


PDF = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n'


class Clock:
    now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response:
    encoding = 'utf-8'

    def __init__(self, content=b'', status=200, **headers):
        self.content, self.status_code, self.headers = content, status, headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP %s' % self.status_code)

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset+chunk_size]


class Session:
    def __init__(self, responses, clock=None):
        self.responses, self.calls, self.clock = responses, [], clock or Clock()
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, self.clock(), kwargs))
        value = self.responses[url]
        response = value.pop(0) if isinstance(value, list) else value
        if isinstance(response, Exception):
            raise response
        return response


class PdfDownloadTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.Logger('test-download')
        self.logger.addHandler(logging.NullHandler())

    def job(self, folder, key, url):
        return DownloadJob({'ID': key}, Path(folder) / (key + '.pdf'), deque([url]))

    def test_per_host_rate_and_queue_fairness(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = Clock()
            jobs = [self.job(folder, 'a', 'https://a.test/1'),
                    self.job(folder, 'b', 'https://a.test/2'),
                    self.job(folder, 'c', 'https://b.test/1')]
            session = Session({j.urls[0]: Response(PDF) for j in jobs}, clock)
            self.assertEqual(download_queue(jobs, session, HostRateLimiter(clock=clock),
                                           self.logger, sleep=clock.sleep), (3, 0))
            self.assertEqual([(u, t) for u, t, _ in session.calls],
                             [('https://a.test/1', 0), ('https://b.test/1', 0),
                              ('https://a.test/2', 30)])
            self.assertTrue(all(Path(j.entry['file']).read_bytes() == PDF for j in jobs))

    def test_redirect_and_discovery_requests_are_rate_limited(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = Clock()
            job = self.job(folder, 'a', 'https://doi.org/10.1/a')
            session = Session({
                'https://doi.org/10.1/a': Response(status=302, Location='https://pub.test/article'),
                'https://pub.test/article': Response(
                    b'<meta name="citation_pdf_url" content="/paper.pdf">'),
                'https://pub.test/paper.pdf': Response(PDF),
            }, clock)
            self.assertEqual(download_queue([job], session, HostRateLimiter(clock=clock),
                                           self.logger, sleep=clock.sleep), (1, 0))
            self.assertEqual([t for _, t, _ in session.calls], [0, 0, 30])
            self.assertTrue(all(not options['allow_redirects'] for _, _, options in session.calls))

    def test_retry_after_and_retry_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = Clock()
            job = self.job(folder, 'a', 'https://pub.test/a')
            session = Session({'https://pub.test/a': [
                Response(status=429, **{'Retry-After': '90'}), Response(PDF)]}, clock)
            self.assertEqual(download_queue([job], session, HostRateLimiter(clock=clock),
                                           self.logger, sleep=clock.sleep), (1, 0))
            self.assertEqual([t for _, t, _ in session.calls], [0, 90])

    def test_failed_or_truncated_responses_never_become_files(self):
        for response in (Response(b'<html>Login required</html>'),
                         Response(b'%PDF-1.4\ntruncated'), Response(status=403)):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as folder:
                job = self.job(folder, 'bad', 'https://pub.test/bad')
                self.assertEqual(download_queue([job], Session({'https://pub.test/bad': response}),
                                               HostRateLimiter(), self.logger), (0, 1))
                self.assertNotIn('file', job.entry)
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_size_limit_and_candidate_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            job = self.job(folder, 'large', 'https://a.test/large')
            result = download_queue([job], Session({'https://a.test/large': Response(PDF)}),
                                    HostRateLimiter(), self.logger, max_bytes=5)
            self.assertEqual(result, (0, 1))
            self.assertFalse(job.destination.exists())
            job = self.job(folder, 'fallback', 'https://a.test/missing')
            job.urls.append('https://b.test/paper.pdf')
            result = download_queue([job], Session({
                'https://a.test/missing': Response(status=404),
                'https://b.test/paper.pdf': Response(PDF)}), HostRateLimiter(), self.logger)
            self.assertEqual(result, (1, 0))

    def test_bibtex_output_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, directory = (Path(folder) / p for p in ('in.bib', 'out.bib', 'pdfs'))
            source.write_text('@article{a, title={Test}, source={Scopus}, url={https://pub.test/a}}')
            session = Session({'https://pub.test/a': Response(PDF)})
            with patch('requests.Session', return_value=session):
                self.assertEqual(download_bibtex(source, output, directory, self.logger), (1, 0, 0))
                self.assertEqual(download_bibtex(output, output, directory, self.logger), (0, 1, 0))
            entry = bibtexparser.loads(output.read_text()).entries[0]
            self.assertEqual(entry['source'], 'Scopus')
            self.assertEqual(Path(entry['file']).read_bytes(), PDF)
            self.assertNotIn('file', bibtexparser.loads(source.read_text()).entries[0])
            self.assertEqual(len(session.calls), 1)

    def test_parse_error_reports_line_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = (Path(folder) / p for p in ('in.bib', 'out.bib', 'errors.log'))
            source.write_text('\n@article{bad, title={oops')
            output.write_text('previous')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = main([str(source), '-o', str(output), '--pdf-dir', str(Path(folder)/'pdfs'),
                               '--log-file', str(log)])
            self.assertEqual(status, 1)
            self.assertIn('in.bib: line 2:', stderr.getvalue())
            self.assertIn('in.bib: line 2:', log.read_text())
            self.assertEqual(output.read_text(), 'previous')

    def test_safe_filenames_and_url_candidates(self):
        self.assertNotEqual(pdf_filename({'ID': 'a/b'}), pdf_filename({'ID': 'a_b'}))
        self.assertNotIn('/', pdf_filename({'ID': '../../x'}))
        self.assertEqual(candidate_urls({'pdf': 'https://a.test/a.pdf', 'doi': '10.1/a_b'}),
                         ['https://a.test/a.pdf', 'https://doi.org/10.1/a_b'])

    def test_acm_uses_only_direct_doi_pdf_url(self):
        for source in ('ACM', 'acm', ' Scopus; aCm '):
            with self.subTest(source=source):
                entry = {'source': source, 'doi': '10.1145/3704637.3734749',
                         'url': 'https://other.test/article', 'pdf': 'https://other.test/paper.pdf'}
                self.assertEqual(candidate_urls(entry),
                                 ['https://dl.acm.org/doi/pdf/10.1145/3704637.3734749'])
        self.assertEqual(candidate_urls({'source': 'ACM', 'url': 'https://other.test/a.pdf'}), [])
        self.assertEqual(candidate_urls({'source': 'ACM', 'doi': 'https://doi.org/10.1/a'}),
                         ['https://dl.acm.org/doi/pdf/10.1/a'])

    def test_acm_never_uses_http_or_html_fallback(self):
        endpoint = 'https://dl.acm.org/doi/pdf/10.1/a'
        for backend in (Mock(side_effect=ValueError('HTTP 403')),
                        Mock(return_value=b'<meta name="citation_pdf_url" content="https://other.test/a.pdf">')):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as folder:
                job = self.job(folder, 'acm', endpoint)
                job.entry['source'] = 'ACM'
                session = Session({})
                self.assertEqual(download_queue([job], session, HostRateLimiter(), self.logger,
                                               browser_download=backend), (0, 1))
                self.assertEqual(session.calls, [])
                backend.assert_called_once_with(endpoint, 30, 100*1024*1024)

    def test_acm_direct_pdf_download(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, directory = (Path(folder) / p for p in ('in.bib', 'out.bib', 'pdfs'))
            source.write_text('@article{a, title={Test}, source={ACM}, doi={10.1/a}, '
                              'url={https://other.test/article}}')
            endpoint = 'https://dl.acm.org/doi/pdf/10.1/a'
            session = Session({})
            backend = Mock(return_value=PDF)
            with patch('requests.Session', return_value=session):
                self.assertEqual(download_bibtex(source, output, directory, self.logger,
                                                browser_download=backend), (1, 0, 0))
            self.assertEqual(session.calls, [])
            backend.assert_called_once_with(endpoint, 30, 100*1024*1024)
            self.assertTrue(Path(bibtexparser.loads(output.read_text()).entries[0]['file']).exists())

    def test_acm_browser_jobs_share_per_host_rate_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = Clock()
            jobs = [self.job(folder, str(n), 'https://dl.acm.org/doi/pdf/10.1/%d' % n)
                    for n in range(2)]
            for job in jobs:
                job.entry['source'] = 'ACM'
            starts = []
            def browser(url, timeout, max_bytes):
                starts.append(clock())
                return PDF
            self.assertEqual(download_queue(jobs, Session({}), HostRateLimiter(clock=clock),
                                           self.logger, browser_download=browser,
                                           sleep=clock.sleep), (2, 0))
            self.assertEqual(starts, [0, 30])


if __name__ == '__main__':
    unittest.main()
