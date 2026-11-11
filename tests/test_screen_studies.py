import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from bibtex_io import read_bibtex
from screen_studies import decide, evaluate, main, read_criteria


def rating(identifier, score, assessable=True, reason='Supported by the study metadata.'):
    return {'id': identifier, 'score': score, 'assessable': assessable, 'reason': reason}


class StudyScreeningTests(unittest.TestCase):
    def test_decisions_use_strict_threshold_and_exclusion_priority(self):
        for inclusion, exclusion, expected in [
            (rating('I1', 6), rating('E1', 5), 'selected'),
            (rating('I1', 7), rating('E1', 6), 'excluded'),
            (rating('I1', 5), rating('E1', 1), 'excluded'),
            (rating('I1', 4, False), rating('E1', 1), 'probably excluded'),
            (rating('I1', 7), rating('E1', 4, False), 'probably excluded'),
            (rating('I1', 4, False), rating('E1', 7), 'excluded'),
        ]:
            with self.subTest(expected=expected, inclusion=inclusion, exclusion=exclusion):
                self.assertEqual(decide([inclusion, exclusion]), expected)
        self.assertEqual(decide([rating('I1', 2), rating('I2', 6)]), 'selected')

    def test_invalid_model_ratings_are_rejected(self):
        session = MagicMock()
        response = session.post.return_value.__enter__.return_value
        for ratings in ([], [rating('I2', 6)], [rating('I1', 8)], [rating('I1', 7, False)]):
            response.json.return_value = {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text',
                                               'text': json.dumps({'ratings': ratings})}]}]}
            with self.subTest(ratings=ratings), self.assertRaises(ValueError):
                evaluate(session, 'test-model', {'title': 'Study'}, {'I1': 'Relevant'})

    def test_cli_screens_only_requested_fields_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            source, rules, output = [Path(folder) / name for name in ('in.bib', 'rules.txt', 'out.bib')]
            source.write_text('@article{a, title={Example}, author={A}, abstract={Evidence}, keywords={Topic}}\n'
                              '@article{b, author={B}}')
            rules.write_text('I1: Includes the target population\nE1 - Editorial\n')
            args = [str(source), '--criteria', str(rules), '-o', str(output), '--model', 'test-model']
            with patch.dict('os.environ', OPENAI_API_KEY='fake-key'), \
                    patch('screen_studies.evaluate', return_value=[rating('I1', 6), rating('E1', 2)]) as scorer, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            scorer.assert_called_once()
            self.assertEqual(scorer.call_args.args[2], {'title': 'Example', 'abstract': 'Evidence', 'keywords': 'Topic'})
            records = read_bibtex(output).entries
            self.assertEqual(records[0]['screening_decision'], 'selected')
            self.assertEqual(records[0]['screening_scores'], 'I1=6, E1=2')
            self.assertEqual(records[0]['author'], 'A')
            self.assertEqual(records[1]['screening_decision'], 'probably excluded')
            self.assertIn('I1=4 (insufficient evidence)', records[1]['screening_scores'])

    def test_criteria_file_reports_duplicate_with_line_number(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'criteria.txt'
            path.write_text('I1 Relevant\nI1 Duplicate\n')
            with self.assertRaisesRegex(ValueError, 'criteria.txt:2: duplicate criterion I1'):
                read_criteria(path)

    def test_cli_uses_selected_provider_credentials_and_structured_response(self):
        response_text = json.dumps({'ratings': [rating('I1', 6)]})
        cases = (
            ('gemini', 'GEMINI', 'x-goog-api-key',
             'https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent',
             {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                 {'thought': True, 'text': 'internal reasoning'}, {'text': response_text}]}}]}),
            ('anthropic', 'ANTHROPIC', 'x-api-key', 'https://api.anthropic.com/v1/messages',
             {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': response_text}]}),
        )
        with tempfile.TemporaryDirectory() as folder:
            source, rules, output = [Path(folder) / name for name in ('in.bib', 'rules.txt', 'out.bib')]
            source.write_text('@article{a, title={Example}}')
            rules.write_text('I1 Relevant\n')
            for provider, prefix, header, endpoint, result in cases:
                with self.subTest(provider=provider), patch.dict('os.environ', {
                        prefix + '_API_KEY': 'provider-key', prefix + '_MODEL': 'test-model',
                        'OPENAI_API_KEY': 'different-key'}), patch('screen_studies.requests.Session') as factory, \
                        contextlib.redirect_stdout(io.StringIO()):
                    session = factory.return_value.__enter__.return_value
                    session.headers = {}
                    session.post.return_value.__enter__.return_value.json.return_value = result
                    status = main([str(source), '--criteria', str(rules), '-o', str(output),
                                   '--provider', provider])
                    self.assertEqual(status, 0)
                    self.assertEqual(session.headers[header], 'provider-key')
                    self.assertNotIn('Authorization', session.headers)
                    self.assertEqual(session.post.call_args.args[0], endpoint)
                    self.assertEqual(read_bibtex(output).entries[0]['screening_decision'], 'selected')

    def test_config_keys_override_environment_and_cli_overrides_config_model(self):
        with tempfile.TemporaryDirectory() as folder:
            source, rules, output, config = [Path(folder) / name for name in
                                             ('in.bib', 'rules.txt', 'out.bib', 'settings.local.ini')]
            source.write_text('@article{a, title={Example}}')
            rules.write_text('I1 Relevant\n')
            config.write_text('[openai]\napi_key=other-key\n[gemini]\napi_key=file%key\nmodel=file-model\n')
            with patch.dict('os.environ', GEMINI_API_KEY='env-key', GEMINI_MODEL='env-model'), \
                    patch('screen_studies.requests.Session') as factory, \
                    patch('screen_studies.evaluate', return_value=[rating('I1', 6)]) as scorer, \
                    contextlib.redirect_stdout(io.StringIO()):
                session = factory.return_value.__enter__.return_value
                session.headers = {}
                args = [str(source), '--criteria', str(rules), '-o', str(output),
                        '--provider', 'gemini', '--config', str(config)]
                self.assertEqual(main(args), 0)
                self.assertEqual(session.headers['x-goog-api-key'], 'file%key')
                self.assertEqual(scorer.call_args.args[1], 'file-model')
                self.assertEqual(main(args + ['--model', 'cli-model']), 0)
                self.assertEqual(scorer.call_args.args[1], 'cli-model')
