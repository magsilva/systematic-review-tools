from collections import deque
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import parse_bibtex, new_database, serialize_bibtex
from browser_pdf import PdfBytes
from download_pdfs import DownloadJob, HostRateLimiter, download_bibtex, download_queue
from paper_discovery import (PdfVerificationError, discovery_url, normalize, rank_candidates, verify_pdf)
from test_download_pdfs import Clock, Response, Session


TITLE = 'Supporting autistic developers in software engineering teams'
ABSTRACT = ('We investigate communication practices among autistic software developers '
            'using interviews and observations across several engineering teams. '
            'Our findings describe accessible collaboration strategies and inclusive project workflows.')


def entry():
    return {'ID': 'paper', 'ENTRYTYPE': 'article', 'title': TITLE,
            'author': 'Miller, Alice and Smith, Bob', 'year': '2024', 'abstract': ABSTRACT}


def metadata(doi='10.1234/paper', url='https://pub.test/paper.pdf', **overrides):
    result = {'DOI': doi, 'title': [TITLE], 'author': [{'family': 'Miller'}, {'family': 'Smith'}],
              'abstract': '<jats:p>' + ABSTRACT + '</jats:p>',
              'published': {'date-parts': [[2024]]},
              'link': [{'URL': url, 'content-type': 'application/pdf'}]}
    result.update(overrides)
    return result


def payload(*items):
    return {'message': {'items': list(items)}}


def pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject(
        {NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    literal = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream.set_data(('BT /F1 10 Tf 50 700 Td (' + literal + ') Tj ET').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


MATCHING_PDF = lambda: pdf(TITLE + '\nAlice Miller and Bob Smith\nAbstract\n' + ABSTRACT)


class PaperDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.Logger('discovery-test')
        self.logger.addHandler(logging.NullHandler())

    def test_metadata_ranking_rejects_wrong_title_authors_year(self):
        good = metadata()
        result = rank_candidates(entry(), payload(
            metadata(title=['Unrelated quantum physics']),
            metadata(author=[{'family': 'Jones'}]), metadata(published={'date-parts': [[2019]]}), good))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['doi'], '10.1234/paper')
        self.assertEqual(rank_candidates({'title': TITLE}, payload(metadata(author=[], abstract=''))), [])

    def test_verifies_real_pdf_text_and_rejects_wrong_authors_abstract_title(self):
        candidate = rank_candidates(entry(), payload(metadata()))[0]
        for text, success in ((TITLE+' Alice Miller Bob Smith '+ABSTRACT, True),
                              (TITLE+' James Jones '+ABSTRACT, False),
                              (TITLE+' Alice Miller Bob Smith Unrelated results on quantum physics.', False),
                              ('Other paper Alice Miller Bob Smith '+ABSTRACT, False), ('', False)):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/'paper.pdf'
                path.write_bytes(pdf(text))
                if success:
                    verify_pdf(path, entry(), candidate)
                else:
                    with self.assertRaises(PdfVerificationError):
                        verify_pdf(path, entry(), candidate)

    def test_lookup_download_verification_and_bibtex_update(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, directory = (Path(folder)/p for p in ('in.bib', 'out.bib', 'pdfs'))
            source.write_text(serialize_bibtex(new_database()))
            database = new_database()
            database.entries = [entry()]
            source.write_text(serialize_bibtex(database))
            original = source.read_bytes()
            lookup, url = discovery_url(entry()), 'https://pub.test/paper.pdf'
            session = Session({lookup: Response(json.dumps(payload(metadata())).encode()),
                               url: Response(MATCHING_PDF())})
            with patch('requests.Session', return_value=session):
                counts = download_bibtex(source, output, directory, self.logger,
                                         browser_download=Mock(), browser_fallback=Mock())
            self.assertEqual(counts, (1, 0, 0))
            result = parse_bibtex(output.read_text()).entries[0]
            self.assertEqual((result['doi'], result['url']), ('10.1234/paper', url))
            self.assertEqual(Path(result['file']).read_bytes(), MATCHING_PDF())
            self.assertEqual(source.read_bytes(), original)

    def test_rejected_candidate_is_discarded_then_next_candidate_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = Clock()
            lookup = discovery_url(entry())
            destination = Path(folder)/'paper.pdf'
            job = DownloadJob(entry(), destination, deque([lookup]), discovery_url=lookup)
            session = Session({lookup: Response(json.dumps(payload(
                metadata(doi='10.1234/wrong', url='https://wrong.test/a.pdf'),
                metadata(doi='10.1234/right', url='https://right.test/a.pdf'))).encode()),
                'https://wrong.test/a.pdf': Response(pdf(TITLE+' Jones '+ABSTRACT)),
                'https://right.test/a.pdf': Response(MATCHING_PDF())}, clock)
            browser = Mock()
            result = download_queue([job], session, HostRateLimiter(clock=clock), self.logger,
                                    browser_fallback=browser, sleep=clock.sleep)
            self.assertEqual(result, (1, 0))
            self.assertEqual(job.entry['doi'], '10.1234/right')
            browser.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [destination])

    def test_verification_failure_preserves_existing_pdf_and_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'paper.pdf'
            destination.write_bytes(MATCHING_PDF())
            original = entry()
            job = DownloadJob(dict(original), destination, deque(['https://pub.test/a.pdf']),
                              active_candidate=rank_candidates(entry(), payload(metadata()))[0])
            result = download_queue([job], Session({'https://pub.test/a.pdf': Response(pdf('Wrong paper'))}),
                                    HostRateLimiter(), self.logger)
            self.assertEqual(result, (0, 1))
            self.assertEqual(destination.read_bytes(), MATCHING_PDF())
            self.assertEqual(job.entry, original)
            self.assertEqual(list(Path(folder).iterdir()), [destination])

    def test_browser_discovery_records_actual_download_url(self):
        with tempfile.TemporaryDirectory() as folder:
            url = 'https://pub.test/article'
            actual_url = 'https://cdn.test/final.pdf'
            job = DownloadJob(entry(), Path(folder)/'a.pdf', deque([url]), browser_urls={url},
                              active_candidate=rank_candidates(entry(), payload(metadata()))[0])
            backend = Mock(return_value=PdfBytes(MATCHING_PDF(), actual_url))
            self.assertEqual(download_queue([job], Session({}), HostRateLimiter(), self.logger,
                                           browser_fallback=backend), (1, 0))
            self.assertEqual(job.entry['url'], actual_url)
            self.assertEqual(job.entry['doi'], '10.1234/paper')

    def test_acm_discovery_downloads_only_acm_pdf_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            original = dict(entry(), publisher='ACM')
            lookup = discovery_url(original)
            job = DownloadJob(original, Path(folder)/'a.pdf', deque([lookup]), discovery_url=lookup)
            session = Session({lookup: Response(json.dumps(payload(metadata())).encode())})
            backend = Mock(return_value=MATCHING_PDF())
            result = download_queue([job], session, HostRateLimiter(), self.logger, browser_download=backend)
            self.assertEqual(result, (1, 0))
            backend.assert_called_once_with('https://dl.acm.org/doi/pdf/10.1234/paper', 30)
            self.assertEqual([u for u, _, _ in session.calls], [lookup])

    def test_excluded_missing_identifier_entry_does_not_trigger_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, directory = (Path(folder)/p for p in ('in.bib', 'out.bib', 'pdfs'))
            source.write_text('@article{a, title={An ACM paper}, publisher={ACM}}')
            session = Session({})
            with patch('requests.Session', return_value=session):
                self.assertEqual(download_bibtex(source, output, directory, self.logger,
                                                exclude_sources=['ACM']), (0, 0, 0))
            self.assertEqual(session.calls, [])

    def test_normalization_handles_jats_accents_and_hyphenated_lines(self):
        self.assertEqual(normalize('<p>{Café} soft-\nware</p>'), 'cafe software')


if __name__ == '__main__':
    unittest.main()
