#!/usr/bin/env python3
"""Build a Boolean search expression from PICO keyword sequences."""

import argparse
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SearchFormat:
    """Rendering rules; subclass the rendering methods for richer syntax.

    Templates receive an already quoted keyword or a rendered clause body.
    Prefixes are literal strings, including any required punctuation/spaces.
    """

    keyword_template: str = '{prefix}{keyword}'
    clause_template: str = '{prefix}({keywords})'
    keyword_prefix: str = ''
    clause_prefix: str = ''
    disjunction: str = ' OR '
    conjunction: str = ' AND '

    def quote_keyword(self, keyword):
        escaped = keyword.replace('\\', '\\\\').replace('"', '\\"')
        return '"' + escaped + '"'

    def render_keyword(self, keyword):
        return self.keyword_template.format(
            prefix=self.keyword_prefix, keyword=self.quote_keyword(keyword)
        )

    def render_clause(self, keywords):
        return self.clause_template.format(
            prefix=self.clause_prefix,
            keywords=self.disjunction.join(self.render_keyword(k) for k in keywords),
        )

    def render(self, clauses):
        return self.conjunction.join(self.render_clause(clause) for clause in clauses)


FORMATS = {
    'canonical': SearchFormat(),
    'scopus': SearchFormat(clause_prefix='TITLE-ABS-KEY:'),
    'acm': SearchFormat(
        keyword_template='[{prefix}{keyword}]',
        clause_template='{prefix}[ {keywords} ]',
        keyword_prefix='All: ',
    ),
}


def build_pico_clauses(population=(), intervention=(), control=(), outcome=()):
    """Normalize PICO components into non-empty sequences of literal keywords."""
    clauses = tuple(
        tuple(keyword.strip() for keyword in keywords if keyword.strip())
        for keywords in (population, intervention, control, outcome)
    )
    clauses = tuple(clause for clause in clauses if clause)
    if not clauses:
        raise ValueError("At least one non-empty keyword is required.")
    return clauses


def build_search_expression(population=(), intervention=(), control=(), outcome=(),
                            *, output_format='canonical', clause_prefix=None,
                            keyword_prefix=None):
    """Return OR clauses joined by AND, omitting empty PICO components.

    Each item is a literal keyword or phrase, not a Boolean subexpression.
    Whitespace-only items are ignored; keyword order is preserved.
    """
    if isinstance(output_format, str):
        try:
            formatter = FORMATS[output_format]
        except KeyError:
            raise ValueError('Unknown output format: ' + output_format) from None
    else:
        formatter = output_format
    overrides = {}
    if clause_prefix is not None:
        overrides['clause_prefix'] = clause_prefix
    if keyword_prefix is not None:
        overrides['keyword_prefix'] = keyword_prefix
    if overrides:
        formatter = replace(formatter, **overrides)
    return formatter.render(build_pico_clauses(population, intervention, control, outcome))


def add_pico_arguments(parser):
    """Share PICO keyword options between expression and search applications."""
    for component in ('population', 'intervention', 'control', 'outcome'):
        parser.add_argument(
            '--' + component, nargs='+', action='extend', default=[],
            metavar='KEYWORD',
            help='Keywords or quoted phrases for %s; may be repeated.' % component,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--format', dest='output_format', choices=FORMATS,
                        default='canonical', help='Output syntax (default: canonical).')
    parser.add_argument('--clause-prefix', default=None,
                        help='Override the prefix before every component clause.')
    parser.add_argument('--keyword-prefix', default=None,
                        help='Override the prefix inside each keyword wrapper.')
    add_pico_arguments(parser)
    args = parser.parse_args(argv)
    try:
        expression = build_search_expression(**vars(args))
    except ValueError as error:
        parser.error(str(error))
    print(expression)


if __name__ == '__main__':
    main()
