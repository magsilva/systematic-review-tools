import contextlib
import io
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import read_bibtex
from extract_citing_papers import OpenAlex, extract, main


def work(identifier, **fields):
    return {'id': 'https://openalex.org/' + identifier, **fields}


class ForwardCitationTests(unittest.TestCase):
    def test_cli_paginates_and_merges_citing_paper_sources(self):
        citing = work('W3', doi='https://doi.org/10.1234/citing',
                      display_name='A citing paper', publication_year=2025, type='article',
                      authorships=[{'author': {'display_name': 'Jane Doe'}}],
                      primary_location={'source': {'display_name': 'Example Conference', 'type': 'conference'}},
                      abstract_inverted_index={'works': [1], 'It': [0]},
                      keywords=[{'display_name': 'Testing'}])
        second = work('W4', display_name='Another citing paper')
        with tempfile.TemporaryDirectory() as folder:
            source, output, log = [Path(folder) / name for name in ('in.bib', 'out.bib', 'log')]
            source.write_text('@article{first, doi={10.1234/first}}\n'
                              '@article{second, title={Second paper}, year={2020}, author={Doe, Jane}}')

            def api(client, url=None, **params):
                if url:
                    return work('W1', doi='https://doi.org/10.1234/first')
                if 'search' in params:
                    return {'results': [work('W2', display_name='Second paper', publication_year=2020,
                                              authorships=[{'author': {'display_name': 'Jane Doe'}}])]}
                if params['filter'] == 'cites:W2':
                    return {'results': [citing], 'meta': {'count': 1}}
                if params['cursor'] == '*':
                    return {'results': [citing], 'meta': {'count': 2, 'next_cursor': 'next'}}
                self.assertEqual(params['cursor'], 'next')
                return {'results': [second], 'meta': {'count': 2}}

            with patch.object(OpenAlex, 'get', autospec=True, side_effect=api), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(source), '-o', str(output), '--log-file', str(log)]), 0)
            records = read_bibtex(output).entries
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]['source'], '10.1234/first; second')
            self.assertEqual(records[0]['ENTRYTYPE'], 'inproceedings')
            self.assertEqual(records[0]['booktitle'], 'Example Conference')
            self.assertEqual(records[0]['author'], 'Jane Doe')
            self.assertEqual(records[0]['abstract'], 'It works')
            self.assertEqual(records[0]['keywords'], 'Testing')
            self.assertEqual(records[1]['openalex_id'], 'W4')

    def test_incomplete_page_retains_records_and_reports_error(self):
        client = OpenAlex(Mock())
        client.resolve = Mock(return_value='W1')
        client.get = Mock(return_value={'results': [work('W2')], 'meta': {'count': 2}})
        logger = logging.getLogger('forward-citation-test')
        with self.assertLogs(logger, level='WARNING'):
            records, stats = extract([{'ID': 'seed'}], client, logger)
        self.assertEqual(records[0]['source'], 'seed')
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(stats['completed'], 0)
