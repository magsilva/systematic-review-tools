import contextlib
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import sol_search
import main as application
from bibtex_io import read_bibtex


class SolSearchTests(unittest.TestCase):
    def test_canonical_query_and_all_collections_preserved(self):
        query = '("autism" OR "Asperger") AND ("software engineering")'
        values = parse_qs(urlparse(sol_search.build_search_url(query, 2)).query)
        self.assertEqual(values['query'], [query])
        self.assertEqual(values['archiveIds[]'], ['1', '2', '3'])
        self.assertEqual(values['searchPage'], ['2'])
        with self.assertRaises(ValueError):
            sol_search.build_search_url(' ')

    def result_page(self, ranges, links, body=''):
        page = Mock()
        page.locator.return_value = Mock()
        locators = {key: Mock() for key in ('a.record_title', '.articles_count', 'body')}
        locators['a.record_title'].evaluate_all.side_effect = links
        locators['.articles_count'].all_text_contents.side_effect = [[value] for value in ranges]
        locators['body'].inner_text.return_value = body
        page.locator.side_effect = locators.__getitem__
        return page

    def test_pagination(self):
        page = self.result_page(['1 a 2 de 3 itens', '3 a 3 de 3 itens'], [['a', 'b'], ['c']])
        self.assertEqual(sol_search.collect_result_urls(page, '"autism"', 5), ['a', 'b', 'c'])
        self.assertIn('searchPage=2', page.goto.call_args.args[0])

    def test_incomplete_changed_and_repeated_pages_fail(self):
        for ranges, links in [(['1 a 2 de 3 itens'], [['a']]),
                              (['1 a 1 de 2 itens', '2 a 2 de 3 itens'], [['a'], ['b']]),
                              (['1 a 1 de 2 itens', '2 a 2 de 2 itens'], [['a'], ['a']])]:
            with self.subTest(ranges=ranges), self.assertRaises(RuntimeError):
                sol_search.collect_result_urls(self.result_page(ranges, links), 'autism', 5)

    def test_zero_results_and_unrecognized_page(self):
        self.assertEqual(sol_search.collect_result_urls(
            self.result_page([''], [[]], 'Nenhum resultado encontrado'), 'autism', 5), [])
        with self.assertRaises(RuntimeError):
            sol_search.collect_result_urls(self.result_page([''], [[]], 'Service unavailable'), 'autism', 5)

    def test_http_error(self):
        page = Mock()
        page.goto.return_value.ok = False
        page.goto.return_value.status = 503
        with self.assertRaisesRegex(RuntimeError, '503'):
            sol_search.visit(page, 'https://sol.sbc.org.br', 5)

    def article_page(self, export=True):
        page = Mock()
        locators = {key: Mock() for key in ('meta[name][content]', '.abstract', '.keywords .value',
            sol_search.CITATION_SELECTOR)}
        locators['meta[name][content]'].evaluate_all.return_value = [
            ['citation_title', 'Autism & software'], ['citation_author', 'Ana Silva'],
            ['citation_author', 'João Souza'], ['citation_conference', 'Simpósio'],
            ['citation_date', '2025/10/01'], ['citation_doi', '10.5753/example']]
        locators['.abstract'].first.count.return_value = 1
        locators['.abstract'].first.inner_text.return_value = 'Resumo\nAn abstract about autism.'
        locators['.keywords .value'].all_text_contents.return_value = ['Autism, software']
        citation = locators[sol_search.CITATION_SELECTOR].first
        citation.count.return_value = int(export)
        citation.get_attribute.return_value = '/index.php/example/article/cite/1/BibtexCitationPlugin'
        page.locator.side_effect = locators.__getitem__
        page.goto.return_value.text.return_value = json.dumps({'content': '<textarea>@inproceedings{same, '
            'title={Autism and software}, author={Silva, Ana}, booktitle={Full Proceedings}, '
            'year={2025}, keywords={}}</textarea>'})
        return page

    def test_citation_and_page_metadata_enrichment(self):
        entry = sol_search.extract_entry(self.article_page(), 'https://sol.sbc.org.br/index.php/example/article/view/1', 5, Mock())
        self.assertEqual(entry['booktitle'], 'Full Proceedings')
        self.assertEqual(entry['author'], 'Silva, Ana')
        self.assertEqual(entry['doi'], '10.5753/example')
        self.assertEqual(entry['abstract'], 'An abstract about autism.')
        self.assertEqual(entry['keywords'], 'Autism, software')
        self.assertEqual(entry['source'], 'SOL')
        self.assertTrue(entry['ID'].startswith('sol_'))

    def test_metadata_fallback_and_safe_serialization(self):
        from bibtex_io import new_database, write_bibtex
        page = self.article_page(export=False)
        logger = Mock()
        entry = sol_search.extract_entry(page, 'https://sol.sbc.org.br/example', 5, logger)
        self.assertEqual(entry['ENTRYTYPE'], 'inproceedings')
        self.assertEqual(entry['author'], 'Ana Silva and João Souza')
        self.assertEqual(entry['title'], r'Autism \& software')
        logger.warning.assert_called_once()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out.bib'
            write_bibtex(new_database([entry]), output)
            self.assertEqual(len(read_bibtex(output).entries), 1)

    def test_malformed_citation_falls_back_with_warning(self):
        page, logger = self.article_page(), Mock()
        page.goto.return_value.text.return_value = 'invalid export'
        entry = sol_search.extract_entry(page, 'https://sol.sbc.org.br/example', 5, logger)
        self.assertEqual(entry['author'], 'Ana Silva and João Souza')
        logger.warning.assert_called_once()

    def test_cli_query_file_and_errors_preserve_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            query, output, log = [Path(folder) / name for name in ('query.txt', 'out.bib', 'log')]
            query.write_text('("autism")')
            output.write_text('previous output')
            with patch('sol_search.search_to_bibtex', side_effect=RuntimeError('navigation failed')), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                status = sol_search.main(['--query-file', str(query), '--output', str(output), '--log-file', str(log)])
            self.assertEqual(status, 1)
            self.assertIn('navigation failed', log.read_text())
            self.assertIn('navigation failed', stderr.getvalue())
            self.assertEqual(output.read_text(), 'previous output')

    def test_input_log_collision_checked_before_opening_log(self):
        with tempfile.TemporaryDirectory() as folder:
            query = Path(folder) / 'query.txt'
            query.write_text('autism')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sol_search.main(['--query-file', str(query), '--output', str(Path(folder)/'out.bib'),
                    '--log-file', str(query)]), 1)
            self.assertEqual(query.read_text(), 'autism')

    def test_export_merges_duplicates_and_closes_browser(self):
        runtime = Mock()
        context = Mock()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out.bib'
            with patch('playwright.sync_api.sync_playwright') as playwright, \
                    patch('sol_search.launch_context', return_value=context), \
                    patch('sol_search.collect_result_urls', return_value=['url1', 'url2']), \
                    patch('sol_search.extract_entry', side_effect=[
                        {'ID': 'a', 'ENTRYTYPE': 'article', 'title': 'Study', 'doi': '10.5753/example', 'source': 'SOL'},
                        {'ID': 'b', 'ENTRYTYPE': 'article', 'title': 'Study', 'doi': '10.5753/example', 'abstract': 'Details', 'source': 'SOL'}]):
                playwright.return_value.__enter__.return_value = runtime
                self.assertEqual(sol_search.search_to_bibtex('autism', output), 1)
            entries = read_bibtex(output).entries
            self.assertEqual(entries[0]['abstract'], 'Details')
            self.assertEqual(entries[0]['source'], 'SOL')
            context.close.assert_called_once()

    def test_extraction_failure_preserves_output_and_closes_browser(self):
        context = Mock()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out.bib'
            output.write_text('previous bibliography')
            with patch('playwright.sync_api.sync_playwright'), \
                    patch('sol_search.launch_context', return_value=context), \
                    patch('sol_search.collect_result_urls', return_value=['url']), \
                    patch('sol_search.extract_entry', side_effect=RuntimeError('incomplete metadata')):
                with self.assertRaisesRegex(RuntimeError, 'incomplete metadata'):
                    sol_search.search_to_bibtex('autism', output)
            self.assertEqual(output.read_text(), 'previous bibliography')
            context.close.assert_called_once()

    def test_main_rejects_other_engine_options_for_sol(self):
        for option in ('--no-browser', '--non-subscriber'):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()), \
                    patch('sol_search.search_to_bibtex') as search:
                with self.assertRaises(SystemExit):
                    application.main(['--engine', 'sol', '--population', 'autism',
                                      '--output', 'out.bib', option])
                search.assert_not_called()

    def test_main_sol_search_and_pico_filter_pipeline(self):
        with tempfile.TemporaryDirectory() as folder:
            output, log = Path(folder) / 'out.bib', Path(folder) / 'log'
            def search(query, path, **kwargs):
                self.assertEqual(query, '("autism") AND ("software engineering")')
                path.write_text('@inproceedings{a, title={Autism software engineering}, source={SOL}}\n'
                                '@book{b, title={Autism software engineering}}')
                return 2
            with patch('sol_search.search_to_bibtex', side_effect=search), contextlib.redirect_stdout(io.StringIO()):
                status = application.main(['--engine', 'SOL', '--population', 'autism',
                    '--intervention', 'software engineering', '--filter-pico', '--include-type', 'inproceedings',
                    '--output', str(output), '--log-file', str(log)])
            self.assertEqual(status, 0)
            entries = read_bibtex(output).entries
            self.assertEqual([e['ID'] for e in entries], ['a'])
            self.assertEqual(entries[0]['source'], 'SOL')


if __name__ == '__main__':
    unittest.main()
