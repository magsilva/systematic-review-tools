from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from browser_pdf import BrowserPdfDownloader
from download_pdfs import HostRateLimiter


PDF = b'%PDF-1.4\n%%EOF\n'


def response(url, body, content_type):
    return Mock(url=url, status=200, headers={'content-type': content_type},
                body=Mock(return_value=body))


class BrowserPdfTests(unittest.TestCase):
    def backend(self, page):
        backend = BrowserPdfDownloader('unused-test-profile')
        backend.runtime = Mock()
        backend.context = Mock(new_page=Mock(return_value=page))
        return backend

    def test_browser_discovers_pdf_and_paces_navigation(self):
        page = Mock(url='https://pub.test/article')
        page.goto.side_effect = [response('https://pub.test/article', b'<html></html>', 'text/html'),
                                 response('https://pub.test/paper.pdf', PDF, 'application/pdf')]
        page.locator.return_value.evaluate_all.return_value = ['/paper.pdf']
        backend = self.backend(page)
        now = [0]
        limiter = HostRateLimiter(clock=lambda: now[0])
        limiter.started('https://pub.test/article')
        def sleep(seconds):
            now[0] += seconds
        with patch('browser_pdf.time.sleep', side_effect=sleep):
            self.assertEqual(backend.download('https://pub.test/article', 30, 1024,
                                             strict=False, limiter=limiter), PDF)
        self.assertEqual(now[0], 30)
        self.assertEqual([call.args[0] for call in page.goto.call_args_list],
                         ['https://pub.test/article', 'https://pub.test/paper.pdf'])
        page.close.assert_called_once()

    def test_strict_acm_rejects_redirect_navigation(self):
        page = Mock()
        page.goto.return_value = response('https://other.test/paper.pdf', PDF, 'application/pdf')
        backend = self.backend(page)
        with self.assertRaisesRegex(ValueError, 'unexpected URL'):
            backend.download('https://dl.acm.org/doi/pdf/10.1/a', 30, 1024)
        handler = page.route.call_args.args[1]
        route = Mock()
        route.request.is_navigation_request.return_value = True
        route.request.url = 'https://other.test/paper.pdf'
        handler(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        page.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
