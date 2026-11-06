import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from pico_search import FORMATS, SearchFormat, build_pico_clauses, build_search_expression


class PicoSearchTests(unittest.TestCase):
    def test_scopus(self):
        self.assertEqual(
            build_search_expression(['adults', 'children'], ['exercise'],
                                    output_format='scopus'),
            'TITLE-ABS-KEY:("adults" OR "children") AND TITLE-ABS-KEY:("exercise")',
        )

    def test_acm(self):
        self.assertEqual(
            build_search_expression(['adults', 'children'], ['exercise'],
                                    output_format='acm'),
            '[ [All: "adults"] OR [All: "children"] ] AND [ [All: "exercise"] ]',
        )

    def test_prefix_overrides_are_local(self):
        self.assertEqual(
            build_search_expression(['adults'], output_format='scopus',
                                    clause_prefix='TITLE:'),
            'TITLE:("adults")',
        )
        self.assertEqual(FORMATS['scopus'].clause_prefix, 'TITLE-ABS-KEY:')
        self.assertEqual(
            build_search_expression(['adults'], output_format='acm',
                                    keyword_prefix='Title: '),
            '[ [Title: "adults"] ]',
        )
        self.assertEqual(
            build_search_expression(['adults'], output_format='acm', keyword_prefix=''),
            '[ ["adults"] ]',
        )

    def test_custom_format_and_reusable_clauses(self):
        custom = SearchFormat(clause_template='{prefix}<{keywords}>', clause_prefix='X:')
        clauses = build_pico_clauses([' adults ', ''], outcome=['health'])
        self.assertEqual(custom.render(clauses), 'X:<"adults"> AND X:<"health">')
        self.assertEqual(
            build_search_expression(['adults'], output_format=custom), 'X:<"adults">'
        )

    def test_unknown_format(self):
        with self.assertRaisesRegex(ValueError, 'Unknown output format'):
            build_search_expression(['adults'], output_format='unknown')

    def test_cli_format_and_prefix(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'src/pico_search.py'), '--format', 'acm',
             '--keyword-prefix', 'Title: ', '--population', 'adults', 'children'],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout,
                         '[ [Title: "adults"] OR [Title: "children"] ]\n')

    def test_four_components(self):
        self.assertEqual(
            build_search_expression(
                ['adults', 'children'], ['exercise'], ['usual care'], ['fitness', 'health']
            ),
            '("adults" OR "children") AND ("exercise") AND ("usual care") '
            'AND ("fitness" OR "health")',
        )

    def test_empty_components_and_whitespace(self):
        self.assertEqual(
            build_search_expression(population=[' ', ' adults '], outcome=['health']),
            '("adults") AND ("health")',
        )

    def test_empty_query(self):
        with self.assertRaises(ValueError):
            build_search_expression(population=[' '])

    def test_literal_quotes_and_backslashes(self):
        self.assertEqual(
            build_search_expression(population=['a"b', 'a\\b']),
            '("a\\"b" OR "a\\\\b")',
        )

    def test_cli_repeated_component(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'src/pico_search.py'),
             '--population', 'adults', '--population', 'children',
             '--outcome', 'quality of life'],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            result.stdout,
            '("adults" OR "children") AND ("quality of life")\n',
        )

    def test_cli_empty_query(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'src/pico_search.py')],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('At least one non-empty keyword', result.stderr)


if __name__ == '__main__':
    unittest.main()
