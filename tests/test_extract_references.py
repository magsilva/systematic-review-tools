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
            args = [str(source), '-o', str(output), '--log-file', str(log)]
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
