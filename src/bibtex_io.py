"""Strict BibTeX parsing with source-file line diagnostics (bibtexparser 1.x)."""


def merge_sources(*values):
    """Return a deterministic, case-insensitive union of semicolon-separated sources."""
    names = {}
    canonical = {'acm': 'ACM', 'scopus': 'Scopus'}
    for value in values:
        for name in value.split(';'):
            name = name.strip()
            if name:
                key = name.casefold()
                names.setdefault(key, canonical.get(key, name))
    return '; '.join(names[key] for key in sorted(names))


class BibtexParseError(ValueError):
    """A parse/validation failure associated with a one-based source line."""

    def __init__(self, source, line, message):
        self.source = str(source)
        self.line = line
        super().__init__('%s: line %d: %s' % (source, line, message))


def parse_bibtex(content, source, *, require_titles=False):
    """Reject skipped content and annotate failures with their original location.

    For malformed records the reported line is the beginning of the rejected
    record/block; parser exceptions use their precise location when available.
    This uses the 1.x expression hooks in one place for both importing and merging.
    """
    from bibtexparser.bparser import BibTexParser

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False

    def fail(text, location, message):
        raise BibtexParseError(source, text.count('\n', 0, location) + 1, message)

    def located(action):
        def run(text, location, tokens):
            try:
                action(tokens)
            except BibtexParseError:
                raise
            except Exception as error:
                fail(text, location, str(error))
        return run

    def entry(tokens):
        if require_titles and not tokens.get('Fields', {}).get('title'):
            raise ValueError('BibTeX entry %s is missing a title.' % tokens.get('Key'))
        parser._add_entry(tokens.get('EntryType'), tokens.get('Key'), tokens.get('Fields'))

    def rejected(text, location, tokens):
        # The parser can include leading blank space in an implicit comment.
        while location < len(text) and text[location].isspace():
            location += 1
        fail(text, location, 'Unparsed content or comments; check the BibTeX record/block starting here.')

    parser._expr.entry.set_parse_action(located(entry))
    parser._expr.string_def.set_parse_action(located(
        lambda t: parser._add_string(t['StringName'].name, t['StringValue'])))
    parser._expr.preamble_decl.set_parse_action(located(lambda t: parser._add_preamble(t[0])))
    parser._expr.implicit_comment.add_parse_action(rejected)
    parser._expr.explicit_comment.add_parse_action(rejected)
    try:
        database = parser.parse(content)
    except BibtexParseError:
        raise
    except Exception as error:
        location = getattr(error, 'loc', 0)
        fail(content, location, str(error))
    if require_titles and not database.entries:
        raise BibtexParseError(source, 1, 'Export must contain BibTeX entries with titles.')
    return database
