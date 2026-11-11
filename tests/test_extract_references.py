import contextlib
import io
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import read_bibtex
from extract_references import Crossref, extract, main


def work(doi, **extra):
    return dict(DOI=doi, title=['A cited paper'], author=[{'family': 'Doe', 'given': 'Jane'}],
                issued={'date-parts': [[2020]]}, **extra)


class ReferenceTests(unittest.TestCase):
    def test_shared_reference_and_unresolved_citation(self):
        client = Mock()
        citation = {'DOI': '10.1234/SHARED'}
        client.work.side_effect = {
            '10.1234/parent': {'reference': [citation]},
            '10.1234/shared': work('10.1234/shared', type='proceedings-article',
                                   abstract='<jats:p>50% &amp; text {</jats:p>',
                                   subject=['Computing'], **{'container-title': ['Conference']}),
        }.__getitem__
        client.find.side_effect = [
            {'reference': [citation, {'unstructured': 'An unmatched citation'}]}, None]
        with self.assertLogs('reference-test', level='WARNING'):
            records, stats = extract([{'ID': 'first', 'doi': '10.1234/parent'},
                                      {'ID': 'second', 'title': 'Second paper'}], client,
                                     logging.getLogger('reference-test'))
        self.assertEqual(len(records), 2)
        self.assertEqual(stats['references'], 3)
        self.assertEqual(stats['unresolved'], 1)
        self.assertEqual(records[0]['source'], '10.1234/parent; second')
        self.assertEqual(records[0]['author'], 'Doe, Jane')
        self.assertEqual(records[0]['title'], 'A cited paper')
        self.assertEqual(records[0]['year'], '2020')
        self.assertEqual(records[0]['booktitle'], 'Conference')
        self.assertEqual(records[0]['keywords'], 'Computing')
        self.assertIn(r'50\% \& text \textbraceleft{}', records[0]['abstract'])
        self.assertEqual(records[1]['note'], 'An unmatched citation')

    def test_search_requires_corroboration_and_rejects_ambiguity(self):
        client = Crossref(Mock())
        matching = work('10.1234/yes')
        client.get = Mock(return_value={'items': [work('10.1234/no'), matching]})
        self.assertIsNone(client.find(title='A cited paper', author='Doe', year='2020'))
        client.get.return_value = {'items': [matching]}
        self.assertIsNone(client.find(title='A cited paper', author='Other'))
        self.assertEqual(client.find(citation='Doe. A cited paper. 2020.'), matching)

    def test_doi_lookup_caches_requests(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=Mock(return_value={'message': work('10.1234/a')}))
        client = Crossref(session, mailto='review@example.org')
        self.assertEqual(client.work('10.1234/a'), client.work('10.1234/a'))
        session.get.assert_called_once_with('https://api.crossref.org/works/10.1234/a',
                                           params={'mailto': 'review@example.org'}, timeout=30)

    def test_cli_writes_new_bibliography_and_preserves_output_on_total_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = [Path(folder) / name for name in ('in.bib', 'out.bib', 'log')]
            source.write_text('@article{parent, doi={10.1234/parent}}')
            args = [str(source), '-o', str(output), '--log-file', str(log), '--no-scholar', '--no-openalex']
            with patch('extract_references.Crossref') as api, contextlib.redirect_stdout(io.StringIO()):
                api.return_value.work.side_effect = [{'reference': [{'DOI': '10.1234/a'}]}, work('10.1234/a')]
                self.assertEqual(main(args), 0)
            self.assertEqual(read_bibtex(output).entries[0]['source'], '10.1234/parent')
            previous = output.read_bytes()
            with patch('extract_references.Crossref') as api, contextlib.redirect_stderr(io.StringIO()):
                api.return_value.work.side_effect = requests.Timeout('offline')
                self.assertEqual(main(args), 1)
            self.assertEqual(output.read_bytes(), previous)
            self.assertIn('offline', log.read_text())

    def test_scholar_fallback_recovers_parent_and_cited_paper(self):
        from extract_references import doi_of, normalized
        from scholar_references import Scholar

        logger = logging.getLogger('scholar-test')
        scholar = Scholar(entries=[
            {'ID': 'parent', 'title': 'Parent paper', 'year': '2022', 'doi': '10.1234/parent'},
            {'ID': 'cited', 'ENTRYTYPE': 'article', 'title': 'A cited paper', 'author': 'Doe, Jane',
             'year': '2020', 'journal': 'Example Journal', 'source': 'prior'},
        ], logger=logger, identify=doi_of, normalize=normalized)
        client = Mock()
        client.find.return_value = None
        client.work.return_value = {'reference': [
            {'article-title': 'A cited paper', 'author': 'Doe', 'year': '2020'}]}
        records, stats = extract([{'ID': 'parent', 'title': 'Parent paper', 'year': '2022'}],
                                 client, logger, scholar)
        client.work.assert_called_once_with('10.1234/parent')
        self.assertEqual(records[0]['source'], '10.1234/parent; prior')
        self.assertEqual(records[0]['author'], 'Doe, Jane')
        self.assertEqual(records[0]['journal'], 'Example Journal')
        self.assertEqual(stats['scholar_matches'], 1)
        self.assertEqual(stats['unresolved'], 0)

    def test_scholar_rejects_conflicting_doi_and_does_not_replace_crossref(self):
        from extract_references import doi_of, normalized
        from scholar_references import Scholar
        logger = logging.getLogger('scholar-test')
        scholar = Scholar(entries=[
            {'ID': 'wrong', 'title': 'Same title', 'year': '2020', 'doi': '10.1234/wrong'},
        ], logger=logger, identify=doi_of, normalize=normalized)
        self.assertIsNone(scholar.find(title='Same title', year='2020', doi='10.1234/right'))
        client, fallback = Mock(), Mock()
        client.work.side_effect = [{'reference': [{'DOI': '10.1234/cited'}]}, work('10.1234/cited')]
        extract([{'ID': 'parent', 'doi': '10.1234/parent'}], client, logger, fallback)
        fallback.find.assert_not_called()

    def test_scholar_is_skipped_for_incomplete_crossref_records_and_request_errors(self):
        client, scholar = Mock(), Mock()
        client.work.side_effect = [
            {'reference': [{'DOI': '10.1234/sparse'}, {'DOI': '10.1234/offline'}]},
            {'DOI': '10.1234/sparse'},
            requests.Timeout('reference lookup timed out'),
            requests.Timeout('parent lookup timed out'),
            {'DOI': '10.1234/no-references'},
        ]
        logger = logging.getLogger('scholar-test')
        with self.assertLogs(logger, level='WARNING'):
            records, stats = extract([
                {'ID': 'parent', 'doi': '10.1234/parent'},
                {'ID': 'offline', 'doi': '10.1234/offline-parent'},
                {'ID': 'empty', 'doi': '10.1234/no-references'},
            ], client, logger, scholar)
        scholar.find.assert_not_called()
        self.assertEqual(len(records), 2)
        self.assertEqual(stats['errors'], 2)
        self.assertEqual(stats['unresolved'], 1)

    def test_online_scholar_matches_verified_export_and_stops_after_block(self):
        from extract_references import doi_of, normalized
        from scholar_references import Scholar
        scholar = Scholar(entries=[], logger=logging.getLogger('scholar-test'),
                          identify=doi_of, normalize=normalized, online=True)
        match = {'ID': 'cited', 'title': 'A cited paper', 'author': 'Doe, Jane', 'year': '2020'}
        scholar._search = Mock(side_effect=[[dict(match, author='Other, Jane'), match],
                                            RuntimeError('verification required')])
        self.assertEqual(scholar.find(title='A cited paper', author='Doe', year='2020'), match)
        with self.assertLogs('scholar-test', level='WARNING'):
            self.assertIsNone(scholar.find(title='Another paper', year='2020'))
        self.assertIsNone(scholar.find(title='Third paper', year='2020'))
        self.assertEqual(scholar._search.call_count, 2)

    def test_browser_reads_matching_scholar_bibtex_export(self):
        from extract_references import doi_of, normalized
        from scholar_references import Scholar
        scholar = Scholar(entries=[], logger=logging.getLogger('scholar-test'),
                          identify=doi_of, normalize=normalized, online=True)
        page = Mock(url='https://scholar.google.com/scholar?q=paper')
        page.goto.return_value.status = 200
        result = Mock()
        heading, cite = Mock(), Mock()
        heading.count.return_value = cite.count.return_value = 1
        heading.inner_text.return_value = 'A cited paper'
        heading.locator.return_value.count.return_value = 0
        result.locator.side_effect = lambda selector: {'.gs_rt': heading, '.gs_or_cit': cite}[selector]
        results = Mock()
        results.count.return_value = 1
        results.nth.return_value = result
        page.locator.side_effect = lambda selector: results if selector == '.gs_r.gs_or.gs_scl' else Mock(count=Mock(return_value=0))
        page.get_by_role.return_value.get_attribute.return_value = 'https://scholar.googleusercontent.com/scholar.bib?q=paper'
        page.request.get.return_value.text.return_value = '@article{a, title={A cited paper}, author={Doe, Jane}, year={2020}}'
        scholar._browser = Mock(return_value=page)
        self.assertEqual(scholar.find(title='A cited paper', author='Doe', year='2020')['ID'], 'a')
        cite.click.assert_called_once()
        page.request.get.assert_called_once()

    def test_openalex_fills_gaps_and_scholar_only_runs_after_both_miss(self):
        crossref, alex, scholar = Mock(), Mock(), Mock()
        crossref.work.side_effect = [
            {'reference': [{'DOI': '10.1234/sparse'}, {'DOI': '10.1234/alex'}, {'DOI': '10.1234/missing'}]},
            work('10.1234/sparse'), None, None,
        ]
        alex.find.side_effect = [
            {'id': 'https://openalex.org/W1', 'display_name': 'Different title',
             'abstract_inverted_index': {'Abstract': [0]}, 'keywords': [{'display_name': 'Topic'}]},
            {'id': 'https://openalex.org/W2', 'display_name': 'OpenAlex match'},
            None,
        ]
        scholar.find.return_value = None
        logger = logging.getLogger('openalex-fallback-test')
        with self.assertLogs(logger, level='WARNING'):
            records, stats = extract([{'ID': 'parent', 'doi': '10.1234/parent'}],
                                     crossref, logger, scholar, alex)
        self.assertEqual(records[0]['title'], 'A cited paper')
        self.assertEqual(records[0]['abstract'], 'Abstract')
        self.assertEqual(records[0]['keywords'], 'Topic')
        self.assertEqual(records[1]['title'], 'OpenAlex match')
        self.assertEqual(stats['openalex_matches'], 2)
        scholar.find.assert_called_once_with(doi='10.1234/missing', title='', author='', year='', citation='')

    def test_openalex_reference_list_when_crossref_list_is_missing(self):
        crossref, alex, scholar = Mock(), Mock(), Mock()
        crossref.work.side_effect = [work('10.1234/parent'), None]
        alex.find.return_value = {'referenced_works': ['https://openalex.org/W1']}
        alex.work.return_value = {'id': 'https://openalex.org/W1', 'doi': 'https://doi.org/10.1234/ref',
                                  'display_name': 'OpenAlex reference'}
        records, stats = extract([{'ID': 'parent', 'doi': '10.1234/parent'}],
                                 crossref, logging.getLogger('openalex-fallback-test'), scholar, alex)
        self.assertEqual(records[0]['doi'], '10.1234/ref')
        self.assertEqual(records[0]['source'], '10.1234/parent')
        self.assertEqual(stats['parents_without_references'], 0)
        scholar.find.assert_not_called()
