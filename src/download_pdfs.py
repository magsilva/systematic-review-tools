#!/usr/bin/env python3
"""Download PDFs referenced by BibTeX entries through a per-host paced queue."""

import argparse
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote, urljoin, urlsplit

from bibtex_io import parse_bibtex


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

    def host(self, url):
        return urlsplit(url).hostname.casefold()

    def delay(self, url):
        return max(0, self.next_allowed.get(self.host(url), 0) - self.clock())

    def started(self, url):
        self.next_allowed[self.host(url)] = self.clock() + self.interval

    def defer(self, url, seconds):
        host = self.host(url)
        self.next_allowed[host] = max(self.next_allowed.get(host, 0), self.clock() + seconds)


@dataclass
class DownloadJob:
    entry: dict
    destination: Path
    urls: deque
    visited: set = field(default_factory=set)
    retries: dict = field(default_factory=dict)
    requests: int = 0
    browser_urls: set = field(default_factory=set)


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


def save_pdf(destination, first, chunks, max_bytes):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            size = len(first)
            if size > max_bytes:
                raise ValueError('PDF exceeds the configured size limit.')
            stream.write(first)
            for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('PDF exceeds the configured size limit.')
                stream.write(chunk)
        if not is_pdf(temporary):
            raise ValueError('Response is not a complete PDF (missing signature or EOF).')
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


def download_queue(jobs, session, limiter, logger, *, timeout=30, max_bytes=100*1024*1024,
                   max_retries=2, sleep=time.sleep, browser_download=None,
                   browser_fallback=None):
    """Process one HTTP request per queue turn; redirects share the same limiter."""
    pending = list(jobs)
    downloaded, failed = 0, 0
    while pending:
        # Rotate jobs after each request so other servers can run during cooldowns.
        position = min(range(len(pending)),
                       key=lambda n: limiter.delay(pending[n].urls[0]) if pending[n].urls else -1)
        job = pending.pop(position)
        if not job.urls or job.requests >= 20:
            logger.error('No PDF downloaded for %s: no usable PDF link or request limit reached',
                         job.entry['ID'])
            failed += 1
            continue
        raw_url = job.urls.popleft()
        url = clean_url(raw_url)
        if url in job.visited:
            pending.append(job)
            continue
        delay = limiter.delay(url)
        if delay:
            job.urls.appendleft(url)
            pending.append(job)
            sleep(min(delay, 60))
            continue
        job.visited.add(url)
        job.requests += 1
        limiter.started(url)
        success = False
        using_browser = is_acm_entry(job.entry) or url in job.browser_urls
        try:
            if using_browser:
                transport = browser_download if is_acm_entry(job.entry) else browser_fallback
                if transport is None:
                    raise RuntimeError('Browser download transport is not available.')
                data = transport(url, timeout, max_bytes)
                save_pdf(job.destination, data, iter(()), max_bytes)
                job.entry['file'] = str(job.destination.resolve())
                downloaded += 1
                logger.info('Saved through browser: %s', job.destination)
                continue
            with session.get(url, stream=True, allow_redirects=False, timeout=timeout) as response:
                if response.status_code in (429, 503):
                    limiter.defer(url, retry_after(response.headers.get('Retry-After', '60')))
                if response.status_code in (429, 500, 502, 503, 504):
                    attempts = job.retries.get(url, 0)
                    if attempts < max_retries:
                        job.retries[url] = attempts + 1
                        job.visited.remove(url)
                        job.urls.appendleft(url)
                        logger.warning('HTTP %s for %s; queued retry %d',
                                       response.status_code, job.entry['ID'], attempts + 1)
                        pending.append(job)
                        continue
                response.raise_for_status()
                if response.status_code in (301, 302, 303, 307, 308):
                    if is_acm_entry(job.entry):
                        raise ValueError('ACM PDF endpoint redirected; no alternative URLs allowed.')
                    location = response.headers.get('Location')
                    if not location:
                        raise ValueError('Redirect has no Location header.')
                    job.urls.appendleft(clean_url(urljoin(url, location)))
                elif response.status_code != 200:
                    raise ValueError('Unexpected HTTP status %s' % response.status_code)
                else:
                    chunks = response.iter_content(chunk_size=8192)
                    first = next(chunks, b'')
                    if b'%PDF-' in first[:1024]:
                        save_pdf(job.destination, first, chunks, max_bytes)
                        success = True
                    else:
                        if is_acm_entry(job.entry):
                            raise ValueError('ACM PDF endpoint returned a non-PDF response; '
                                             'no alternative URLs allowed.')
                        body = bytearray(first)
                        for chunk in chunks:
                            body.extend(chunk)
                            if len(body) > 2*1024*1024:
                                raise ValueError('Non-PDF page exceeds 2 MiB discovery limit.')
                        links = PdfLinks()
                        links.feed(body.decode(response.encoding or 'utf-8', errors='replace'))
                        for link in reversed(links.links[:10]):
                            job.urls.appendleft(clean_url(urljoin(url, link)))
                        if not links.links:
                            raise ValueError('Response is not a PDF and advertises no PDF link.')
        except Exception as error:
            logger.error('Download failed for %s at %s: %s', job.entry['ID'], url, error)
            if not using_browser and browser_fallback is not None and url not in job.browser_urls:
                job.browser_urls.add(url)
                job.visited.discard(url)
                job.urls.appendleft(url)
                logger.info('Queued browser fallback for %s at %s', job.entry['ID'], url)
        if success:
            job.entry['file'] = str(job.destination.resolve())
            downloaded += 1
            logger.info('Saved %s', job.destination)
        else:
            pending.append(job)
    return downloaded, failed


def write_bibtex(database, output):
    import bibtexparser
    from bibtexparser.bwriter import BibTexWriter
    writer = BibTexWriter()
    writer.contents = ['preambles', 'entries']
    writer.order_entries_by = None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(bibtexparser.dumps(database, writer=writer))
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def download_bibtex(source, output, directory, logger, *, rate=2, timeout=30,
                    max_bytes=100*1024*1024, max_retries=2,
                    browser_profile=Path('.pdf-browser'), browser_download=None,
                    sources=None, exclude_sources=None, browser_fallback=None):
    import requests
    source, output, directory = Path(source), Path(output), Path(directory)
    database = parse_bibtex(source.read_text(encoding='utf-8-sig'), source)
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
        if destination.exists():
            if is_pdf(destination):
                entry['file'] = str(destination.resolve())
                resumed += 1
                continue
            raise ValueError('Existing destination is not a PDF: ' + str(destination))
        urls = []
        for url in candidate_urls(entry):
            try:
                urls.append(clean_url(url))
            except ValueError as error:
                logger.error('%s: %s', entry['ID'], error)
        jobs.append(DownloadJob(entry, destination, deque(urls)))
    try:
        with ExitStack() as stack:
            if browser_download is None or browser_fallback is None:
                from browser_pdf import BrowserPdfDownloader
                browser = stack.enter_context(BrowserPdfDownloader(browser_profile))
                if browser_download is None:
                    browser_download = browser.download
                if browser_fallback is None:
                    browser_fallback = lambda url, timeout, max_bytes: browser.download(
                        url, timeout, max_bytes, strict=False, limiter=limiter)
            session = stack.enter_context(requests.Session())
            session.headers.update({'User-Agent': 'systematic-review-tools/pdf-downloader',
                                    'Accept': 'application/pdf,text/html;q=0.9,*/*;q=0.5'})
            downloaded, failed = download_queue(jobs, session, limiter, logger,
                timeout=timeout, max_bytes=max_bytes, max_retries=max_retries,
                browser_download=browser_download, browser_fallback=browser_fallback)
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
    parser.add_argument('--max-size-mb', type=positive_number, default=100)
    parser.add_argument('--max-retries', type=int, default=2)
    parser.add_argument('--log-file', type=Path, default=Path('download_pdfs.log'))
    parser.add_argument('--browser-profile', type=Path, default=Path('.pdf-browser'),
                        help='Isolated persistent browser profile for ACM downloads.')
    args = parser.parse_args(argv)
    if args.max_retries < 0:
        parser.error('--max-retries must not be negative.')
    logger = logging.Logger('download_pdfs')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stderr = logging.StreamHandler(sys.stderr)
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
            timeout=args.timeout, max_bytes=int(args.max_size_mb*1024*1024),
            max_retries=args.max_retries, browser_profile=args.browser_profile,
            sources=args.sources, exclude_sources=args.exclude_sources)
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
