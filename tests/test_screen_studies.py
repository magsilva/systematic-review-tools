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
