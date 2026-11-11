# Systematic review tools

All BibTeX tools use `bibtexparser==2.1.0` (python-bibtexparser) and require
Python 3.10 or newer. Install the shared dependency with:

```sh
.venv/bin/python -m pip install -r requirements-bibtex.txt
```

The ACM, Scopus, and PDF-download requirements include this shared pin.
`src/bibtex_io.py` centralizes v2 parsing, serialization, and atomic UTF-8
file writes for search exports, merging, filtering, downloading, and the legacy
CSV/mapping tools. Application records remain dictionaries; the shared module
converts them to and from v2 `Library`, `Entry`, and `Field` objects. Invalid
blocks are rejected with the input filename and a one-based line number.


## Search, filter, and download from the main application

`src/main.py` supports an optional pipeline after a Scopus search or ACM import.
Use `--include-type` and/or `--exclude-type` for paper types, `--filter-pico`
to require the supplied PICO terms in title/abstract/keywords, and
`--download-pdfs` to download only the retained entries:

```sh
.venv/bin/python src/main.py --engine scopus \
  --population autism ADHD neuroinclusive \
  --intervention "software engineering" "software engineer" \
  --include-type article inproceedings incollection book \
  --filter-pico --download-pdfs --pdf-dir pdfs --output papers-with-files.bib
```

To process an existing bibliography without a search, replace `--engine` with
`--input`:

```sh
.venv/bin/python src/main.py --input neurodivergent-sw-eng.bib \
  --population autism ADHD neuroinclusive \
  --intervention "software engineering" "software engineer" \
  --include-type article inproceedings incollection book \
  --filter-pico --download-pdfs --exclude-source ACM \
  --pdf-dir pdfs --output selected-with-files.bib
```

These examples use abbreviated keyword lists; supply all desired terms as in
`pico_search.py`. PICO arguments build the search expression; they also filter
metadata when `--filter-pico` is supplied. Filtering and downloading are optional,
so existing search-only commands retain their behavior. With `--input`, keywords
are optional unless `--filter-pico` is enabled. No default type restriction is
applied. Input and output files must differ in input-file mode.

Download controls include `--source`, `--exclude-source`, `--rate-limit` (default
2 requests per minute per hostname), `--timeout`, `--max-retries`,
`--browser-profile`, `--show-browser`, and `--no-progress`. The default PDF
directory is `pdfs`. Install the search engine requirements and
`requirements-browser.txt` and Camoufox as described in the PDF-download section
when enabling PDF downloads.

The final output contains retained entries and their downloaded `file` fields.
Search/filter intermediates are temporary. Search or filtering failures preserve
an existing destination; the downloader saves progress even when some downloads
fail and returns a nonzero status. All stages use `--log-file` (default
`<engine>_search.log`, or `review_pipeline.log` with `--input`). ACM processing
continues only after a BibTeX export has actually been imported.

## Extract cited papers

Use the [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
to export the direct references of papers in a bibliography:

```sh
.venv/bin/python -m pip install -r requirements-references.txt
.venv/bin/python src/extract_references.py systematic-search-filtered.bib \
  --output cited-papers.bib --mailto reviewer@example.org
```

`--mailto` is optional; use your own address for Crossref's polite pool. The
script looks up each input paper by DOI or DOI resolver URL. When no DOI is
available, it searches by bibliographic metadata and accepts a single
corroborated candidate from the returned results. It enriches deposited citations
by DOI or bibliographic lookup. Authors, title, journal or booktitle, DOI, year,
abstract, and keywords are written when available. Crossref subject labels are
used when keywords are absent.

Each output entry's `source` contains the citing papers' DOIs (or BibTeX keys
when unavailable), separated by semicolons. Duplicate DOIs and identical
unresolved citations combine their sources. Unmatched citations are retained
with their deposited details; raw citation text is stored in `note` when
available. Only deposited reference lists are covered: a missing list does not
mean that the paper has no references. The script does not access full text.

OpenAlex fills missing fields in Crossref citations and looks up works that
Crossref could not identify. Crossref metadata takes priority. If Crossref
has no reference list for an input paper, the extractor tries OpenAlex's
indexed references. It uses `OPENALEX_API_KEY` when set. Use `--no-openalex`
to disable this fallback.

When Crossref and OpenAlex complete their lookups without identifying a work,
the extractor searches Google Scholar with Playwright and Camoufox. Install
`requirements-browser.txt` and run `.venv/bin/python -m camoufox fetch` to
enable browser lookups. The script checks up to five results and validates a
matching BibTeX citation before using it. It stops automated Scholar lookups
if the site blocks access or requests verification.

Use `--scholar-bibtex scholar-citations.bib` to check local exports first,
`--scholar-manual` to limit lookup to supplied exports, or `--no-scholar`
to disable the fallback entirely. Runs proceed unattended, logging lookup links
for unresolved items and retaining deposited citations. A Scholar citation can
fill metadata or recover a DOI for a Crossref retry. Availability of a parent's
reference list, abstracts, and keywords still depends on the metadata sources.

Requests are sequential and cached only during the run. Use `--interval`
(default 0.5 seconds) and `--timeout` (default 30 seconds) to tune API access.
Warnings and errors go to stderr and `extract_references.log` (or `--log-file`).
Request failures produce a nonzero exit status and any available partial results;
if requests fail and no references are recovered, an existing output is
preserved. Input and output must differ. Output is written atomically.

## Find papers that cite a bibliography

```sh
.venv/bin/python -m pip install -r requirements-references.txt
.venv/bin/python src/extract_citing_papers.py systematic-search-filtered.bib \
  --output citing-papers.bib
```

The script resolves each input in OpenAlex by DOI or OpenAlex ID, or by a
unique title match corroborated by author or year. It follows OpenAlex's
incoming citation links and paginates through the results. Set
`OPENALEX_API_KEY` in your environment for searches that need multiple pages.
See [OpenAlex API authentication](https://help.openalex.org/api/authentication/).

The output includes authors, title, venue, DOI, year, abstract, and OpenAlex
keywords when available. Records are deduplicated by DOI or, when absent,
OpenAlex ID. The `source` field lists the cited input DOIs or BibTeX keys.
Coverage and missing fields depend on the OpenAlex index. Unmatched inputs or
request errors are logged and return a nonzero exit code. Available results
are written to a separate BibTeX file even if some lookups fail.

## Screen studies against inclusion and exclusion criteria

Create a UTF-8 criteria file with one criterion per line and at least one
inclusion criterion. IDs must start with `I` or `E` and be unique.

```text
I1: Studies neurodivergent people working in software engineering.
E1: The publication is an editorial or an opinion piece.
```

Install `requirements-references.txt` and choose an API provider and a model
that supports structured JSON output. OpenAI is the default provider.

| `--provider` | API key environment variable | Model environment variable |
| --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` |
| `gemini` | `GEMINI_API_KEY` | `GEMINI_MODEL` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` |

`--model` overrides the selected provider's model environment variable.
For Anthropic keys that require a workspace, set `ANTHROPIC_WORKSPACE_ID`.

```sh
.venv/bin/python src/screen_studies.py studies.bib --criteria criteria.txt \
  --provider gemini --model YOUR_GEMINI_MODEL --output screened.bib
```

To store credentials in a file, copy `screening.example.ini` to
`settings.local.ini` and fill in `api_key` and `model` under the provider you
use. Git ignores `settings.local.ini`. Restrict access to that file because
it contains plaintext keys.

```sh
.venv/bin/python src/screen_studies.py studies.bib --criteria criteria.txt \
  --provider gemini --config settings.local.ini --output screened.bib
```

The selected provider's configuration takes precedence over its environment
variables. An explicit `--model` takes precedence over both. For Anthropic,
`workspace_id` can be set in the `[anthropic]` section.

The script sends each study's title, abstract, and keywords along with the
criteria to the selected provider. The output keeps every entry and adds
`screening_decision`, `screening_scores`, and `screening_reasons`. An exclusion
score of 6 or 7 overrides any inclusion. Selection requires at least one
inclusion score of 6 or 7 and enough evidence to assess every exclusion.
If the available evidence leaves the decision unresolved, the script records
`probably excluded`. It marks each unassessable criterion separately with
a score of 4. An API failure or invalid response ends the run with an error
before replacing the output.

## Set operations on BibTeX files

Use `src/bibtex_sets.py` with two or more input files:

```sh
.venv/bin/python src/bibtex_sets.py union first.bib second.bib -o union.bib
.venv/bin/python src/bibtex_sets.py intersection first.bib second.bib -o shared.bib
.venv/bin/python src/bibtex_sets.py difference first.bib second.bib -o only-first.bib
.venv/bin/python src/bibtex_sets.py symmetric-difference first.bib second.bib -o exclusive.bib
```

`merge` is an alias for `union`. Intersection requires membership in every input;
difference subtracts all later inputs from the first. Symmetric difference keeps
publications appearing in an odd number of input files. Duplicates within one
file count once for membership.

Two populated DOI fields take priority: equal normalized DOIs match, and distinct
DOIs never match. DOI prefixes, URL encoding, and letter case are normalized.
When a DOI field is absent, DOI resolver URLs are also recognized. If either
record lacks a DOI, matching uses equal titles and author fields, or editor
fields when authors are absent. Author lists are not compared against editor
lists. When both abstracts are present, they must also match. Anonymous records
can also match when all bibliographic metadata is identical (excluding citation
keys, DOI, source, and local file fields), provided a title and at least one of
year, ISBN, ISSN, publisher, URL, or abstract are present. Title alone is insufficient. Text comparison
decodes TeX, normalizes Unicode, and ignores case and whitespace differences.
Author order and name formatting remain significant; citation keys alone never
identify publications. This is conservative matching, without fuzzy similarity.

Every record in a duplicate group must match an incoming record, preventing a
record with missing identifiers or abstracts from bridging conflicting records.
If it fits multiple groups, the first compatible group wins. Output follows
first appearance; missing metadata is filled from duplicates, source fields are
combined, and conflicting values keep the first nonempty value with a warning.
Citation-key collisions are renamed and crossref/xref references are remapped;
references to excluded records generate warnings. Input metadata is not modified.

Output must differ from every input and is written atomically using
`bibtexparser==2.1.0`. Preambles from the inputs are retained. Errors are printed
to stderr and `bibtex_sets.log` (override with `--log-file`), including source line
numbers for parsing failures. Exit status is 0 on success and 1 on failure.
Python callers can use `same_publication`, `set_entries`, or `set_files`.

## Filter BibTeX entries by criteria

Install `requirements-bibtex.txt` if needed, then include only selected types:

```sh
.venv/bin/python src/filter_bibtex.py papers.bib --output selected.bib \
  --include-type inproceedings article
```

Or exclude types:

```sh
.venv/bin/python src/filter_bibtex.py papers.bib --output selected.bib \
  --exclude-type book
```

Both options accept multiple values and can be repeated. Entry type names are
case-insensitive; use names without `@`. Exclusion takes priority over inclusion.
With no inclusion option, all types are eligible; with neither option, all entries
are copied. The console reports counts read, kept, and excluded. An empty result
is valid and produces an empty bibliography. The output must differ from the
input; it is written atomically, and its parent directory must already exist.

Entry order, citation keys, fields, and preambles are preserved semantically;
formatting may change and string macros are expanded. References to filtered-out
entries are preserved and produce warnings. Errors go to stderr and
`filter_bibtex.log` (override with `--log-file`); parsing errors include the file
and line number. Exit status is 0 on success and 1 on failure.

For additional criteria, implement `Criterion.accepts(entry)` and pass instances
to `filter_entries(entries, criteria)` or `filter_file(input, output, criteria)`.
Every criterion must accept an entry for it to be retained. `EntryTypeCriterion`
provides the current include/exclude type rules.

Filter by PICO terms in the title, abstract, and keywords:

```sh
.venv/bin/python src/filter_bibtex.py papers.bib --output selected.bib \
  --include-type article inproceedings \
  --population autism ADHD \
  --intervention "software engineering" "software development"
```

Use `--population`, `--intervention`, `--control`, and `--outcome` with one or
more terms; each option may be repeated. At least one term from **each supplied
component** must occur in any of the three fields. Different components may
match different fields. Omitted components and blank terms impose no restriction.
PICO criteria and entry-type criteria must both pass.

As in the legacy mapping filter, matching uses case-insensitive literal
substrings, including quoted phrases; it does not interpret regular expressions
or Boolean operators. A phrase must occur within a single field. Missing fields
are treated as empty. TeX markup is decoded for matching, while original entry
metadata is retained in the output. Python callers can pass
`PicoCriterion(population=['autism'], intervention=['software engineering'])`
alongside other criteria to `filter_entries` or `filter_file`.

## Download PDFs from a BibTeX file

The CLI displays tqdm progress bars by default: an entry counter with downloaded,
failed, already-saved, and excluded counts, plus the current title, DOI, URL, and
a byte transfer bar. Retries do not count as failed entries until all options
are exhausted. HTTP transfers show byte progress and percentages when the server
provides a content length. Browser transfers show an indeterminate waiting bar
until the browser returns the file, then its final byte count; browser APIs do
not expose streaming byte progress here. Long metadata is shortened to fit the
terminal. Progress goes to stderr, with log messages printed through tqdm to
avoid disrupting the bars. Use `--no-progress` for plain logging.

```sh
.venv/bin/python -m pip install -r requirements-pdf-download.txt
.venv/bin/python src/download_pdfs.py neurodivergent-sw-eng.bib \
  --output neurodivergent-sw-eng-with-files.bib --pdf-dir ./pdfs
```

Restrict downloads by source with `--source`:

```sh
.venv/bin/python src/download_pdfs.py papers.bib --output papers-with-files.bib \
  --pdf-dir ./pdfs --source ACM
```

Use `--source ACM Scopus` or repeat `--source` to allow multiple sources.
Matching ignores case and surrounding whitespace and accepts any source in an
entry's semicolon-separated set. Inferred ACM sources based on publisher also
qualify. Entries with missing/unknown sources are skipped when a filter is
specified. Skipped entries remain in the output with their existing metadata
and file fields unchanged; they make no requests and do not count as failures.
The skipped count is logged. Without `--source`, all entries are considered.
The filter controls eligibility only: an entry with `ACM; Scopus` still uses
ACM browser download rules even when selected through `--source Scopus`.

Exclude sources with `--exclude-source`:

```sh
.venv/bin/python src/download_pdfs.py papers.bib --output papers-with-files.bib \
  --pdf-dir ./pdfs --exclude-source ACM
```

You can list multiple excluded sources or repeat the option. Exclusions take
priority over `--source`: `--source Scopus --exclude-source ACM` skips entries
with `ACM; Scopus` as well. Matching is case-insensitive and includes inferred
ACM sources. With exclusions alone, entries without a source remain eligible.
Excluded entries are preserved unchanged in the output and do not count as failures.

For entries whose semicolon-separated `source` includes ACM (case-insensitive),
the downloader uses a visible browser to retrieve only
`https://dl.acm.org/doi/pdf/<DOI>`. Other URL fields are
ignored, and redirects or HTML discovery are not followed. ACM PDF retrieval
requires a DOI, either already present or verified through title discovery.
This also applies to `source = {ACM; Scopus}`.
When `source` is absent and the publisher is `ACM` or
`Association for Computing Machinery` (case-insensitive), the downloader adds
`source = {acm}` to the output and applies these same rules. Existing source
fields, including empty fields, are not overwritten. Inference also applies to
reused PDFs and entries that fail to download.
Entries with no DOI, URL, or explicit PDF link use the title lookup described
below before being considered unresolved. If a DOI is discovered for an ACM
entry, only its ACM PDF endpoint is used. ACM entries never fall back to the
HTTP client for PDF retrieval (title metadata lookup uses Crossref HTTP).
Install browser support first:

```sh
.venv/bin/python -m pip install -r requirements-browser.txt
.venv/bin/python -m camoufox fetch
```

### Finding missing DOIs and download links

When `doi`, `url`, `pdf`, and `pdf_url` are all missing/blank, the downloader
queries the [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
using the entry's title. It evaluates up to five results, requiring at least
0.92 normalized title similarity and rejecting conflicting publication years
and author lists when both records provide them. Explicit PDF links and DOI
landing pages from accepted candidates enter the existing download queue.
Metadata requests obey the same per-host rate limit. Source exclusions are
applied before discovery, so excluded entries trigger no lookup.

Discovered PDFs are staged in temporary files and checked with
[pypdf text extraction](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)
before being committed. Verification checks the first three pages for the
title near the beginning (exact normalized match or at least 0.90 similarity),
at least 75% of expected author surnames in the header text, and at least 65%
of the abstract's normalized word pairs. Expected authors and abstract come
from the BibTeX entry when available, otherwise from the matched Crossref
record. At least one author and an abstract of eight words are required.
These are conservative text-based heuristics, not a guarantee of identity;
scanned PDFs, unusual layouts, or missing metadata may remain unresolved.
No OCR is performed.

Only a verified download adds its discovered DOI (if available), sets `url`
to the actual PDF download URL, and sets `file` to its saved path. Original
author/abstract metadata is preserved. Rejected PDFs are discarded and the next
candidate is tried; entries and existing PDFs remain unchanged if verification
fails. Existing PDFs belonging to entries without identifiers are not accepted
as a resume shortcut: title discovery and verification must still succeed.
Already-identified entries continue to use their existing download behavior.
Crossref may not index every paper or supply a usable full-text link or abstract.

The console script uses a dedicated **headless Camoufox** session by default,
so download attempts do not open browser windows. Add `--show-browser` to use
a visible browser for troubleshooting or interactive login. It uses a profile
at `.pdf-browser` (override with `--browser-profile`). It does not share the
Codex in-app browser's login session. Browser navigation/attachment responses
are saved and validated before `file` is set. Browser login or security checks
can still prevent automatic retrieval; these failures are logged and receive
no HTTP fallback. Browser download attempts obey the same queue rate limit.

For other entries, the downloader tries `pdf`, `pdf_url`, `url`, and DOI
in that order. DOI links are resolved through doi.org. HTML landing pages are
inspected for `citation_pdf_url` metadata and explicit PDF links; JavaScript is
not executed by the HTTP client. If an HTTP attempt fails (including invalid PDF
responses or landing pages without PDF links), that URL is queued for one browser
attempt. Transient HTTP status retries are exhausted first. The fallback opens
the same URL in the dedicated browser, allowing normal redirects and inspecting
rendered PDF metadata/links. Those discovered links are retrieved in the browser,
without switching back to HTTP. Failed attempts for the same entry are retried
immediately, including HTTP retries, alternative candidate URLs, and browser
fallback. Server-provided `Retry-After` delays are still honored. Browser-managed subresources and automatic
redirect hops are not individually paced. Failed fallbacks are logged and remaining
candidate URLs are tried; there is no endless HTTP/browser retry loop.
Install `requirements-browser.txt` and Playwright with Camoufox for this fallback too.
Access requiring a browser login, CAPTCHA, or unavailable
subscription may still fail and is logged. This script does not guarantee that every
paper has an accessible PDF.

Downloads are paced **per hostname**, with a default limit of **2 requests per
minute** (30 seconds between request starts), except that retries following a
failure for the same entry bypass this interval. Each retry updates the cooldown
for the next entry. Landing pages, redirects, and successful discovery steps use
normal pacing. Different servers have independent limits; the queue processes
ready jobs while another server is cooling down. Limits apply to one script
process, not across multiple concurrent runs. To change the rate:

```sh
.venv/bin/python src/download_pdfs.py papers.bib --output papers-with-files.bib \
  --pdf-dir ./pdfs --rate-limit 4
```

The script honors `Retry-After` on HTTP 429/503 and retries transient HTTP
429/500/502/503/504 responses up to `--max-retries` times (default 2). Individual
requests use `--timeout` seconds (default 30).
There is no PDF size limit (`--max-size-mb` has been removed). HTTP PDFs are
streamed to disk; browser transfers may be buffered in memory by the browser
backend. Landing pages are limited to 2 MiB, and each entry
has a maximum of 20 requests to bound redirects and discovery loops.

Files have sanitized citation-key names with a hash suffix to avoid collisions.
Responses must have a PDF signature and EOF marker before being saved
atomically; this rejects HTML/login pages and obvious truncation but does not
fully validate PDF internals. Each successful entry receives, for example:

```bibtex
file = {/absolute/path/to/pdfs/citation-key-0123456789abcdef.pdf}
```

Existing valid PDFs at the generated destination are reused on subsequent runs
and annotated without network requests. Existing invalid destinations cause an
error rather than being overwritten. The input file is preserved when output
differs; passing the same path updates the BibTeX file in place atomically.
BibTeX formatting/string macros may be rewritten, while entry metadata is
retained. Failed entries receive no new `file` field; existing `file` fields are
preserved. The updated bibliography is saved after the queue finishes and on
Ctrl-C, retaining completed downloads. The output's parent directory must exist;
the PDF directory is created automatically.

Failures go to stderr and `download_pdfs.log` (override with `--log-file`).
Parsing errors include the input filename and line number. Exit status is 0
when all entries succeeded or were reused, 1 on any failure, and 130 on Ctrl-C.
No PDFs are downloaded automatically when installing or testing this script.

## Merge BibTeX files

```sh
.venv/bin/python -m pip install -r requirements-bibtex.txt
.venv/bin/python src/merge_bibtex.py scopus.bib acm.bib --output merged.bib
```

Input order establishes priority. Duplicate records are matched by normalized
DOI (including DOI URLs), Scopus EID, or an exact normalized title plus year.
Title normalization ignores case, protective braces, and whitespace. Different
nonempty DOIs or Scopus EIDs prevent a merge even if titles match. Citation keys
alone never establish identity. Records without sufficient matching metadata
remain separate; this is not fuzzy title matching.

Duplicates contribute missing/blank fields to the first record. Conflicting
nonempty fields retain the first value and generate warnings; `misc` entry types
are upgraded when a more specific type is available. Author lists and abstracts
are treated as complete fields, not concatenated. Citation key collisions on
distinct records receive numeric suffixes. `crossref` and `xref` references are
updated using their original input file's keys. Ambiguous duplicate keys within
one input file cause an error unless the records can be identified as duplicates.

The `source` field is merged as a set rather than treated as a conflict.
Scopus exports include `source = {Scopus}`, and ACM imports include
`source = {ACM}`. A duplicate found in both engines becomes
`source = {ACM; Scopus}`. Source names are separated by semicolons, trimmed,
deduplicated without regard to case, and sorted for consistent output. Existing
source names are preserved, with ACM and Scopus spelling standardized. Missing
sources in older files are not inferred from filenames or publishers.

The script expands BibTeX string macros, preserves Unicode and distinct preambles,
and writes a UTF-8 file atomically. Input files must differ from the output file.
Empty input files are allowed. Unparsed content/comments cause an error to avoid
silently discarding malformed entries; remove comments before merging. Unresolved
references produce warnings. Errors and conflict warnings go to stderr and
`merge_bibtex.log` (override with `--log-file`). Exit status is 0 on success and
1 on failure. The console reports the input, output, and merged duplicate counts.

BibTeX parsing errors in merges and ACM imports include the input filename and
one-based line number in both stderr and the log, for example
`papers.bib: line 42: Unparsed content or comments ...`. For a malformed entry
or block, the line identifies its beginning; missing-title and macro-expansion
errors identify the related entry/declaration. These diagnostics retain original
line numbers when literal braces are normalized during merging.

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
supported for API retrieval; ACM supports the browser-assisted workflow below.
For Scopus, it selects the API query syntax automatically and delegates
retrieval, BibTeX export, and error logging to `scopus_search.py`.
The `--config-path`, `--refresh`, and `--non-subscriber` options are also
available. Exit status is 0 on success, 1 on search/export failure, and 2 for
invalid command-line input. Search/export errors go to the log and stderr;
command-line validation errors go to stderr.

To support another engine, add an adapter to `SEARCH_ENGINES` in `src/main.py`.
Each adapter receives parsed options and PICO keyword sequences, generates
the engine's expression with `pico_search`, invokes its search runner, and
returns its exit status.

### ACM browser-assisted search

Install the BibTeX import dependency:

```sh
.venv/bin/python -m pip install -r requirements-acm.txt
```

Generate the canonical query and open ACM in your default browser:

```sh
.venv/bin/python src/main.py --engine acm \
  --population "software engineering" --intervention autism \
  --output acm_papers.bib
```

In the browser, verify that ACM interpreted the query and check the result count.
Sign in if needed. Use **Export Citations**, select **All results** if available,
choose **BibTeX**, and download the export. Enter the downloaded file's path at
the console prompt. If ACM requires batches, combine the exported BibTeX files
before importing. This workflow relies on ACM's web search interface, whose URL
and query interpretation can change; the printed canonical query can also be
pasted into ACM manually.

For a remote/headless console, use `--no-browser` and open the printed URL on
your own computer. Non-interactive runs print instructions without waiting.
To finish later, repeat the keyword arguments with the downloaded export:

```sh
.venv/bin/python src/main.py --engine acm \
  --population "software engineering" --intervention autism \
  --output acm_papers.bib --import-bibtex /path/to/download.bib
```

The standalone runner accepts canonical expressions through `--query` or
`--query-file`:

```sh
.venv/bin/python src/acm_search.py --query-file query.txt \
  --output acm_papers.bib --no-browser
```

ACM errors are recorded in `acm_search.log` and stderr (override with
`--log-file`). A successful preparation exits 0 but explicitly reports that
no papers have been imported; a successful import reports the parsed record
count. Invalid imports exit 1 and preserve existing output. Imports must be
UTF-8 BibTeX with titled entries and no unparsed text/comments. Exported fields
are retained semantically, with an added ACM source (unioned with any existing
sources). The importer rewrites BibTeX formatting and expands string macros.
Validation cannot establish that an export
matches the query or contains every result; verify those details in ACM.

No dltHub dependency or ACM API credentials are needed: the
[dltHub ACM example](https://dlthub.com/context/source/acm-digital-library)
contains a placeholder endpoint, rather than a working search API. Browser
login, query review, and citation export remain manual steps.

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

Web search and browser PDF retrieval use Camoufox through Playwright. Install
`requirements-browser.txt`, then run `.venv/bin/python -m camoufox fetch`.
Search sessions use a separate `.acm-search-camoufox` profile; browser caching
is disabled. ACM search windows stay open while the interactive CLI waits for
the citation export path. Use `--no-browser` to prepare a URL without launching.

OpenAlex clients automatically read `[openalex]` → `api_key` from
`settings.local.ini` in the current working directory. A nonempty file value
takes precedence over `OPENALEX_API_KEY`. If the file or key is absent or the
key is blank, the environment variable is used.
