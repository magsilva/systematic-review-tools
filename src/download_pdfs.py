#!/usr/bin/env python3
"""Download PDFs referenced by BibTeX entries through a per-host paced queue."""

import argparse
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote, urljoin, urlsplit

from bibtex_io import read_bibtex, write_bibtex
from paper_discovery import (PdfVerificationError, discovery_url, rank_candidates, verify_pdf)


def clean_url(value):
    value = re.sub(r'\\([_&%#{}])', r'\1', value.strip())
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('Expected an HTTP(S) URL: ' + value)
    return value


def infer_source(entry):
    """Infer ACM provenance from the publisher only when source is absent."""
    if 'source' not in entry:
        publisher = ' '.join(entry.get('publisher', '').replace('{', '').replace('}', '').split())
        if publisher.casefold() in ('acm', 'association for computing machinery'):
            entry['source'] = 'acm'


def is_acm_entry(entry):
    infer_source(entry)
    return any(source.strip().casefold() == 'acm'
               for source in entry.get('source', '').split(';'))


def matches_sources(entry, sources):
    """Match any selected source, including inferred ACM, without changing skipped entries."""
    if sources is None:
        return True
    effective = dict(entry)
    infer_source(effective)
    names = {name.strip().casefold() for name in effective.get('source', '').split(';')
             if name.strip()}
    return bool(names.intersection(sources))


def candidate_urls(entry):
    urls = []
    acm = is_acm_entry(entry)
    if not acm:
        for name in ('pdf', 'pdf_url', 'url'):
            if entry.get(name):
                urls.append(entry[name])
    if entry.get('doi'):
        doi = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '',
                     entry['doi'].strip(), flags=re.I)
        doi = re.sub(r'\\([_&%#{}])', r'\1', doi)
        if doi.strip():
            base = 'https://dl.acm.org/doi/pdf/' if acm else 'https://doi.org/'
            urls.append(base + quote(doi.strip(), safe='/():;._-'))
    return list(dict.fromkeys(urls))


class PdfLinks(HTMLParser):
    """Read citation_pdf_url metadata and explicit PDF links, without executing JS."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name', '').casefold() == 'citation_pdf_url':
            if attrs.get('content'):
                self.links.insert(0, attrs['content'])
        elif tag in ('a', 'link') and attrs.get('href'):
            if (attrs.get('type', '').casefold() == 'application/pdf'
                    or urlsplit(attrs['href']).path.casefold().endswith('.pdf')):
                self.links.append(attrs['href'])


class HostRateLimiter:
    """Space request starts by 60/rate seconds, independently for each hostname."""

    def __init__(self, rate=2, clock=time.monotonic):
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError('Rate must be a positive finite number.')
        self.interval = 60 / rate
        self.clock = clock
        self.next_allowed = {}
        self.server_backoff = {}

    def host(self, url):
        return urlsplit(url).hostname.casefold()

    def delay(self, url):
        return max(self.retry_delay(url), self.next_allowed.get(self.host(url), 0) - self.clock())

    def retry_delay(self, url):
        return max(0, self.server_backoff.get(self.host(url), 0) - self.clock())

    def started(self, url):
        self.next_allowed[self.host(url)] = self.clock() + self.interval

    def defer(self, url, seconds):
        host = self.host(url)
        self.next_allowed[host] = max(self.next_allowed.get(host, 0), self.clock() + seconds)
        self.server_backoff[host] = max(self.server_backoff.get(host, 0), self.clock() + seconds)


@dataclass
class DownloadJob:
    entry: dict
    destination: Path
    urls: deque
    visited: set = field(default_factory=set)
    retries: dict = field(default_factory=dict)
    requests: int = 0
    browser_urls: set = field(default_factory=set)
    immediate_retry: bool = False
    discovery_url: str = None
    candidates: deque = field(default_factory=deque)
    active_candidate: dict = None


def pdf_filename(entry):
    key = entry['ID']
    stem = re.sub(r'[^A-Za-z0-9._-]', '_', key).strip('._')[:100] or 'paper'
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    return '%s-%s.pdf' % (stem, digest)


def is_pdf(path):
    try:
        with Path(path).open('rb') as stream:
            header = stream.read(1024)
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 2048))
            return b'%PDF-' in header and b'%%EOF' in stream.read()
    except OSError:
        return False


def save_pdf(destination, first, chunks, progress=None, verifier=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(first)
            if progress is not None:
                progress.advance(len(first))
            for chunk in chunks:
                stream.write(chunk)
                if progress is not None:
                    progress.advance(len(chunk))
        if not is_pdf(temporary):
            raise ValueError('Response is not a complete PDF (missing signature or EOF).')
        if verifier is not None:
            if progress is not None:
                progress.status('Verifying title, authors and abstract')
            verifier(temporary)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def retry_after(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    return max(0, seconds) if math.isfinite(seconds) else 60


@dataclass
class AttemptResult:
    downloaded_url: str = None
    retry: bool = False


def candidate_verifier(job):
    if job.active_candidate is None:
        return None
    return lambda path: verify_pdf(path, job.entry, job.active_candidate)


def download_with_browser(job, url, transport, timeout, progress=None):
    """Retrieve a PDF through the selected browser transport and validate it."""
    if transport is None:
        raise RuntimeError('Browser download transport is not available.')
    data = transport(url, timeout)
    if progress is not None:
        progress.total_bytes(len(data))
    save_pdf(job.destination, data, iter(()), progress, candidate_verifier(job))
    return AttemptResult(downloaded_url=getattr(data, 'url', url))


def read_discovery_body(chunks, first=b'', progress=None):
    """Bound metadata/HTML discovery responses, independently of PDF downloads."""
    body = bytearray(first)
    if progress is not None:
        progress.advance(len(first))
    for chunk in chunks:
        body.extend(chunk)
        if progress is not None:
            progress.advance(len(chunk))
        if len(body) > 2*1024*1024:
            raise ValueError('Metadata or HTML response exceeds the 2 MiB discovery limit.')
    return body


def lookup_by_title(job, response, logger, progress=None):
    """Convert a successful Crossref response into provisional paper candidates."""
    if response.status_code != 200:
        raise ValueError('Title lookup returned HTTP %s' % response.status_code)
    body = read_discovery_body(response.iter_content(chunk_size=8192), progress=progress)
    job.candidates.extend(rank_candidates(job.entry, json.loads(body)))
    if not job.candidates:
        logger.warning('No title match with sufficient author/abstract evidence for %s', job.entry['ID'])
    return AttemptResult()


def discover_pdf_links(job, url, response, first, chunks, progress=None):
    """Extract PDF links from a publisher landing page."""
    if is_acm_entry(job.entry):
        raise ValueError('ACM PDF endpoint returned a non-PDF response; no alternative URLs allowed.')
    body = read_discovery_body(chunks, first, progress)
    links = PdfLinks()
    links.feed(body.decode(response.encoding or 'utf-8', errors='replace'))
    for link in reversed(links.links[:10]):
        job.urls.appendleft(clean_url(urljoin(url, link)))
    if not links.links:
        raise ValueError('Response is not a PDF and advertises no PDF link.')
    return AttemptResult()


def schedule_http_retry(job, url, response, limiter, logger, max_retries):
    if response.status_code in (429, 503) and response.headers.get('Retry-After'):
        limiter.defer(url, retry_after(response.headers['Retry-After']))
    if response.status_code not in (429, 500, 502, 503, 504):
        return False
    attempts = job.retries.get(url, 0)
    if attempts >= max_retries:
        return False
    job.retries[url] = attempts + 1
    job.visited.discard(url)
    job.urls.appendleft(url)
    job.immediate_retry = True
    logger.warning('HTTP %s for %s; queued retry %d', response.status_code, job.entry['ID'], attempts + 1)
    return True


def follow_http_redirect(job, url, response):
    if is_acm_entry(job.entry):
        raise ValueError('ACM PDF endpoint redirected; no alternative URLs allowed.')
    location = response.headers.get('Location')
    if not location:
        raise ValueError('Redirect has no Location header.')
    job.urls.appendleft(clean_url(urljoin(url, location)))
    return AttemptResult()


def download_with_http(job, url, session, limiter, logger, timeout, max_retries, progress=None):
    """Perform one HTTP turn: retry, title lookup, redirect, PDF, or landing page."""
    with session.get(url, stream=True, allow_redirects=False, timeout=timeout) as response:
        if schedule_http_retry(job, url, response, limiter, logger, max_retries):
            return AttemptResult(retry=True)
        response.raise_for_status()
        if url == job.discovery_url:
            return lookup_by_title(job, response, logger, progress)
        if response.status_code in (301, 302, 303, 307, 308):
            return follow_http_redirect(job, url, response)
        if response.status_code != 200:
            raise ValueError('Unexpected HTTP status %s' % response.status_code)
        chunks = response.iter_content(chunk_size=8192)
        first = next(chunks, b'')
        if progress is not None:
            progress.total_bytes(response.headers.get('Content-Length'))
        if b'%PDF-' in first[:1024]:
            save_pdf(job.destination, first, chunks, progress, candidate_verifier(job))
            return AttemptResult(downloaded_url=getattr(response, 'url', url))
        return discover_pdf_links(job, url, response, first, chunks, progress)


def activate_next_candidate(job, logger):
    while not job.urls and job.candidates:
        job.active_candidate = job.candidates.popleft()
        candidate = job.active_candidate
        if is_acm_entry(job.entry):
            urls = (['https://dl.acm.org/doi/pdf/' + quote(candidate['doi'], safe='/():;._-')]
                    if candidate['doi'] else [])
        else:
            urls = candidate['urls']
        job.urls.extend(urls)
        logger.info('Trying title match for %s: %s (DOI %s)', job.entry['ID'],
                    candidate['title'], candidate['doi'] or '(unavailable)')


def handle_attempt_failure(job, url, error, using_browser, browser_fallback, logger, progress=None):
    if progress is not None:
        progress.status('Attempt failed; trying next option')
    logger.error('Download failed for %s at %s: %s', job.entry['ID'], url, error)
    job.immediate_retry = True
    if isinstance(error, PdfVerificationError):
        job.urls.clear()
        logger.warning('Rejected unverified PDF for %s; trying the next title match', job.entry['ID'])
    elif (url != job.discovery_url and not using_browser and browser_fallback is not None
          and url not in job.browser_urls):
        job.browser_urls.add(url)
        job.visited.discard(url)
        job.urls.appendleft(url)
        logger.info('Queued browser fallback for %s at %s', job.entry['ID'], url)


def record_download(job, url, logger, progress=None):
    if job.active_candidate is not None:
        if job.active_candidate['doi']:
            job.entry['doi'] = job.active_candidate['doi']
        job.entry['url'] = url
    job.entry['file'] = str(job.destination.resolve())
    if progress is not None:
        progress.finished(True)
    logger.info('Saved %s', job.destination)


def download_queue(jobs, session, limiter, logger, *, timeout=30,
                   max_retries=2, sleep=time.sleep, browser_download=None,
                   browser_fallback=None, progress=None):
    """Schedule rate-limited attempts and dispatch HTTP/browser download strategies."""
    pending = list(jobs)
    downloaded, failed = 0, 0

    def delay_for(job):
        if not job.urls:
            return -1
        delay = (limiter.retry_delay(job.urls[0]) if job.immediate_retry
                 else limiter.delay(job.urls[0]))
        return -2 if job.immediate_retry and delay == 0 else delay

    while pending:
        position = min(range(len(pending)), key=lambda n: delay_for(pending[n]))
        job = pending.pop(position)
        activate_next_candidate(job, logger)
        if not job.urls or job.requests >= 20:
            logger.error('No PDF downloaded for %s: no usable PDF link or request limit reached', job.entry['ID'])
            failed += 1
            if progress is not None:
                progress.finished(False)
            continue
        url = clean_url(job.urls.popleft())
        if url in job.visited:
            pending.append(job)
            continue
        delay = limiter.retry_delay(url) if job.immediate_retry else limiter.delay(url)
        if delay:
            if progress is not None:
                progress.start(job.entry, url, 'Rate limit: waiting %.0fs' % delay)
            job.urls.appendleft(url)
            pending.append(job)
            sleep(min(delay, 60))
            continue
        job.visited.add(url)
        job.requests += 1
        job.immediate_retry = False
        limiter.started(url)
        discovering = url == job.discovery_url
        using_browser = not discovering and (is_acm_entry(job.entry) or url in job.browser_urls)
        if progress is not None:
            display_entry = dict(job.entry)
            if job.active_candidate is not None and job.active_candidate['doi']:
                display_entry['doi'] = job.active_candidate['doi']
            progress.start(display_entry, url, 'Title lookup' if discovering else
                           ('Browser (waiting for file)' if using_browser else 'HTTP'))
        try:
            if using_browser:
                transport = browser_download if is_acm_entry(job.entry) else browser_fallback
                result = download_with_browser(job, url, transport, timeout, progress)
            else:
                result = download_with_http(job, url, session, limiter, logger, timeout, max_retries, progress)
        except Exception as error:
            handle_attempt_failure(job, url, error, using_browser, browser_fallback, logger, progress)
            result = AttemptResult()
        if result.downloaded_url is not None:
            record_download(job, result.downloaded_url, logger, progress)
            downloaded += 1
        elif result.retry or job.immediate_retry:
            pending.insert(0, job)
        else:
            pending.append(job)
    return downloaded, failed


def download_bibtex(source, output, directory, logger, *, rate=2, timeout=30,
                    max_retries=2,
                    browser_profile=Path('.pdf-browser'), browser_download=None,
                    sources=None, exclude_sources=None, browser_fallback=None,
                    browser_headless=True, show_progress=False):
    import requests
    source, output, directory = Path(source), Path(output), Path(directory)
    database = read_bibtex(source)
    limiter = HostRateLimiter(rate)
    directory.mkdir(parents=True, exist_ok=True)
    seen, jobs = set(), []
    resumed, skipped = 0, 0
    selected_sources = ({name.strip().casefold() for name in sources if name.strip()}
                        if sources is not None else None)
    excluded_sources = {name.strip().casefold() for name in (exclude_sources or ())
                        if name.strip()}
    for entry in database.entries:
        if entry['ID'] in seen:
            raise ValueError('Duplicate citation key: ' + entry['ID'])
        seen.add(entry['ID'])
        if (matches_sources(entry, excluded_sources)
                or not matches_sources(entry, selected_sources)):
            skipped += 1
            continue
        infer_source(entry)
        destination = directory / pdf_filename(entry)
        needs_discovery = not any(entry.get(name, '').strip() for name in ('doi', 'url', 'pdf', 'pdf_url'))
        if destination.exists():
            if is_pdf(destination):
                if not needs_discovery:
                    entry['file'] = str(destination.resolve())
                    resumed += 1
                    continue
            else:
                raise ValueError('Existing destination is not a PDF: ' + str(destination))
        urls = []
        for url in candidate_urls(entry):
            try:
                urls.append(clean_url(url))
            except ValueError as error:
                logger.error('%s: %s', entry['ID'], error)
        lookup = discovery_url(entry) if needs_discovery and entry.get('title', '').strip() else None
        if lookup:
            urls = [lookup]
        jobs.append(DownloadJob(entry, destination, deque(urls), discovery_url=lookup))
    try:
        with ExitStack() as stack:
            progress = None
            if show_progress:
                from download_progress import DownloadProgress
                progress = stack.enter_context(DownloadProgress(len(jobs), resumed, skipped))
            if browser_download is None or browser_fallback is None:
                from browser_pdf import BrowserPdfDownloader
                browser = stack.enter_context(BrowserPdfDownloader(
                    browser_profile, headless=browser_headless))
                if browser_download is None:
                    browser_download = lambda url, timeout: browser.download(
                        url, timeout, progress=progress)
                if browser_fallback is None:
                    browser_fallback = lambda url, timeout: browser.download(
                        url, timeout, strict=False, limiter=limiter,
                        progress=progress)
            session = stack.enter_context(requests.Session())
            session.headers.update({'User-Agent': 'systematic-review-tools/pdf-downloader',
                                    'Accept': 'application/pdf,text/html;q=0.9,*/*;q=0.5'})
            downloaded, failed = download_queue(jobs, session, limiter, logger,
                timeout=timeout, max_retries=max_retries,
                browser_download=browser_download, browser_fallback=browser_fallback,
                progress=progress)
    finally:
        # Preserve completed file annotations even on Ctrl-C or an unexpected failure.
        write_bibtex(database, output)
    if sources is not None or exclude_sources is not None:
        logger.info('Skipped %d entries due to source filters.', skipped)
    return downloaded, resumed, failed


def positive_number(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('Must be a positive finite number.')
    return result


def source_name(value):
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError('Source name must not be empty.')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', '-o', type=Path, required=True, help='Updated BibTeX file.')
    parser.add_argument('--pdf-dir', type=Path, required=True)
    parser.add_argument('--source', dest='sources', nargs='+', action='extend',
                        type=source_name, metavar='NAME',
                        help='Only download entries matching any selected source; may be repeated.')
    parser.add_argument('--exclude-source', dest='exclude_sources', nargs='+', action='extend',
                        type=source_name, metavar='NAME',
                        help='Skip entries matching any excluded source; overrides --source; may be repeated.')
    parser.add_argument('--rate-limit', type=positive_number, default=2,
                        help='Requests per minute per hostname (default: 2).')
    parser.add_argument('--timeout', type=positive_number, default=30)
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--log-file', type=Path, default=Path('download_pdfs.log'))
    parser.add_argument('--browser-profile', type=Path, default=Path('.pdf-browser'),
                        help='Isolated persistent browser profile for ACM downloads.')
    parser.add_argument('--show-browser', action='store_true',
                        help='Show Chromium instead of using the default headless browser.')
    parser.add_argument('--no-progress', action='store_true', help='Disable tqdm progress displays.')
    args = parser.parse_args(argv)
    if args.max_retries < 0:
        parser.error('--max-retries must not be negative.')
    logger = logging.Logger('download_pdfs')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    if args.no_progress:
        stderr = logging.StreamHandler(sys.stderr)
    else:
        from download_progress import ProgressLoggingHandler
        stderr = ProgressLoggingHandler()
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)
    if args.log_file.resolve() in (args.input.resolve(), args.output.resolve()):
        logger.error('Log file must differ from BibTeX input and output.')
        return 1
    try:
        log = logging.FileHandler(args.log_file, encoding='utf-8')
    except OSError:
        logger.exception('Unable to open download log')
        return 1
    log.setFormatter(formatter)
    logger.addHandler(log)
    try:
        downloaded, resumed, failed = download_bibtex(
            args.input, args.output, args.pdf_dir, logger, rate=args.rate_limit,
            timeout=args.timeout,
            max_retries=args.max_retries, browser_profile=args.browser_profile,
            sources=args.sources, exclude_sources=args.exclude_sources,
            browser_headless=not args.show_browser, show_progress=not args.no_progress)
        print('Downloaded %d; already saved %d; failed %d. BibTeX: %s' %
              (downloaded, resumed, failed, args.output))
        return 1 if failed else 0
    except KeyboardInterrupt:
        logger.error('Interrupted; completed downloads have been recorded.')
        return 130
    except Exception:
        logger.exception('PDF download failed')
        return 1
    finally:
        log.close()


if __name__ == '__main__':
    sys.exit(main())
