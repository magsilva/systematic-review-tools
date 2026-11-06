# Systematic review tools

## Console application

Run the complete keyword-to-BibTeX workflow with:

```sh
python3 src/main.py --engine scopus \
  --population adults children \
  --intervention exercise "physical activity" \
  --control "usual care" \
  --outcome fitness "quality of life" \
  --output papers.bib --log-file scopus_search.log
```

Install and configure pybliometrics as described below before searching.
The application accepts the same PICO keyword options as `pico_search.py`,
including repeated options and quoted phrases. Only `scopus` is currently
supported. It selects the Scopus API query syntax automatically and delegates
retrieval, BibTeX export, and error logging to `scopus_search.py`.
The `--config-path`, `--refresh`, and `--non-subscriber` options are also
available. Exit status is 0 on success, 1 on search/export failure, and 2 for
invalid command-line input. Search/export errors go to the log and stderr;
command-line validation errors go to stderr.

To support another engine, add an adapter to `SEARCH_ENGINES` in `src/main.py`.
Each adapter receives parsed options and PICO keyword sequences, generates
the engine's expression with `pico_search`, invokes its search runner, and
returns its exit status.

## Scopus search and BibTeX export

Install the search dependency (Python 3.10+):

```sh
python3 -m pip install -r requirements-scopus.txt
python3 -c 'import pybliometrics; pybliometrics.init()'
```

The initialization step configures your Elsevier API key, optional institutional
token, and local cache. Access depends on your Scopus API entitlements and
institutional network. See the [pybliometrics configuration guide](https://pybliometrics.readthedocs.io/en/stable/configuration.html).

Generate a query file, then execute it:

```sh
python3 src/pico_search.py --format scopus --clause-prefix TITLE-ABS-KEY \
  --population adults children --intervention exercise > query.txt
python3 src/scopus_search.py --query-file query.txt \
  --output papers.bib --log-file scopus_search.log
```

The API uses `TITLE-ABS-KEY(...)`; the prefix override above removes the
colon from the configurable display format. Alternatively, supply
`--query 'TITLE-ABS-KEY("adults") AND TITLE-ABS-KEY("exercise")'` directly.
Queries are passed unchanged to `ScopusSearch`.

Use `--config-path` for an alternate configuration, `--refresh` to bypass
cached results, or `--non-subscriber` for STANDARD metadata and non-subscriber
limits. By default the script uses subscriber access and the library's cache.
See [ScopusSearch documentation](https://pybliometrics.readthedocs.io/en/stable/reference/scopus/ScopusSearch.html).

The UTF-8 BibTeX export includes available search metadata: authors, title,
publication, year, DOI, volume, issue, pages, article number, abstract, keywords,
ISSN, and Scopus EID. Missing fields are omitted; no per-paper abstract retrieval
requests are made. Entry types are mapped from Scopus document subtypes.
Citation keys include the Scopus identifier and result position for uniqueness.
An empty search produces an empty BibTeX file. Successful exports replace the
destination atomically; failures preserve an existing export.

Search, configuration, conversion, and output errors are logged with tracebacks
to the log file and stderr; the script exits with status 1. If the log file
cannot be opened, it reports that failure to stderr and stops. Output and log
parent directories must already exist.

## PICO search expressions

Run `src/pico_search.py` with Python 3.8 or newer. Supply keywords for any
of the Population, Intervention, Control, and Outcome components:

```sh
python3 src/pico_search.py \
  --population adults children \
  --intervention exercise "physical activity" \
  --control "usual care" \
  --outcome fitness "quality of life"
```

Output:

```text
("adults" OR "children") AND ("exercise" OR "physical activity") AND ("usual care") AND ("fitness" OR "quality of life")
```

Each component forms a parenthesized disjunction (OR). The components are
joined by AND, producing conjunctive normal form. All keywords are quoted
as literal terms or phrases; embedded quotes and backslashes are escaped
with a backslash. This is a generic Boolean expression; database-specific
field tags and escaping rules may require adaptation.

Options can be repeated to append more keywords. Omitted components and
blank keywords are ignored. At least one non-blank keyword is required.
Quote multi-word phrases in the shell so each phrase is a single argument.

The script also exposes `build_search_expression(population=(),
intervention=(), control=(), outcome=())` for Python callers. Each argument
is a sequence of keyword strings.

### Digital library formats

Choose a format with `--format canonical|scopus|acm`. Canonical remains the
default. The library formats implement the syntax described below:

```sh
python3 src/pico_search.py --format scopus --population adults children --intervention exercise
# TITLE-ABS-KEY:("adults" OR "children") AND TITLE-ABS-KEY:("exercise")

python3 src/pico_search.py --format acm --population adults children --intervention exercise
# [ [All: "adults"] OR [All: "children"] ] AND [ [All: "exercise"] ]
```

Use `--clause-prefix 'TITLE:'` to replace Scopus's default clause prefix.
Use `--keyword-prefix 'Title: '` to replace ACM's default `All: ` prefix
inside each keyword's square brackets. Prefix strings are inserted literally;
include punctuation and spaces as needed. An empty string removes a prefix.
Both overrides apply only to the current call.

### Adding formats

`build_pico_clauses(...)` normalizes keywords independently of output syntax,
so its result can be rendered repeatedly by different formatters.
`SearchFormat` defines keyword quoting, keyword and clause templates, prefixes,
and Boolean separators. Add a named instance to `FORMATS` to expose a new
format through the command line, or pass an instance directly in Python:

```python
from pico_search import SearchFormat, build_search_expression

custom = SearchFormat(clause_prefix='TITLE:')
expression = build_search_expression(
    population=['adults', 'children'], output_format=custom,
)
```

Keyword templates support `{prefix}` and `{keyword}`; clause templates support
`{prefix}` and `{keywords}`. For a library requiring different escaping or
more complex syntax, subclass `SearchFormat` and override `quote_keyword`,
`render_keyword`, `render_clause`, or `render`. The Python API also accepts
`output_format='scopus'` or `'acm'`, plus `clause_prefix` and `keyword_prefix`
overrides.

Run the tests with:

```sh
python3 -m unittest discover -s tests
```
