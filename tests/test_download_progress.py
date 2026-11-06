from collections import deque
import io
import logging
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from download_progress import DownloadProgress
from download_pdfs import DownloadJob, HostRateLimiter, download_queue
from test_download_pdfs import PDF, Response, Session


class DownloadProgressTests(unittest.TestCase):
    def test_counts_metadata_and_byte_progress(self):
        output = io.StringIO()
        with DownloadProgress(2, resumed=1, skipped=3, file=output) as progress:
            progress.start({'ID': 'a', 'title': 'Example title', 'doi': '10.1/a'},
                           'https://pub.test/a.pdf', 'HTTP')
            progress.total_bytes(10)
            progress.advance(4)
            progress.advance(6)
            self.assertEqual((progress.transfer.n, progress.transfer.total), (10, 10))
            progress.finished(True)
            progress.finished(False)
            self.assertEqual((progress.overall.n, progress.overall.total), (3, 3))
            self.assertEqual((progress.downloaded, progress.failed), (1, 1))
            progress.start({'ID': 'b'}, 'https://pub.test/b', 'Browser')
            self.assertIsNone(progress.transfer.total)
        text = output.getvalue()
        for expected in ('Entries to download: 2', 'Example title', '10.1/a',
                         'https://pub.test/a.pdf', 'downloaded=1', 'failed=1'):
            self.assertIn(expected, text)

    def test_queue_counts_only_final_outcomes(self):
        with tempfile.TemporaryDirectory() as folder, \
                DownloadProgress(2, file=io.StringIO()) as progress:
            jobs = [DownloadJob({'ID': 'a', 'title': 'A'}, Path(folder)/'a.pdf',
                                deque(['https://pub.test/a.pdf'])),
                    DownloadJob({'ID': 'b'}, Path(folder)/'b.pdf', deque())]
            result = download_queue(jobs, Session({'https://pub.test/a.pdf': Response(PDF)}),
                                    HostRateLimiter(), logging.getLogger('progress-test'),
                                    progress=progress)
            self.assertEqual(result, (1, 1))
            self.assertEqual((progress.downloaded, progress.failed, progress.overall.n), (1, 1, 2))


if __name__ == '__main__':
    unittest.main()
