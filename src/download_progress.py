"""tqdm console displays for entry counts and the current PDF transfer."""

import logging
import shutil
import sys

from tqdm import tqdm


class ProgressLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            tqdm.write(self.format(record), file=sys.stderr)
        except Exception:
            self.handleError(record)


class DownloadProgress:
    def __init__(self, pending, resumed=0, skipped=0, *, file=None):
        self.file = file if file is not None else sys.stderr
        self.downloaded, self.failed = 0, 0
        self.resumed, self.skipped = resumed, skipped
        self.overall = tqdm(total=pending + resumed, initial=resumed, desc='Entries',
                            unit='entry', position=0, dynamic_ncols=True, file=self.file)
        self.labels = [tqdm(total=0, position=n, bar_format='{desc}', leave=False,
                            file=self.file) for n in (1, 2, 3)]
        self.transfer = tqdm(total=None, desc='Waiting', unit='B', unit_scale=True,
                             position=4, leave=False, dynamic_ncols=True, file=self.file)
        self.counts()
        tqdm.write('Entries to download: %d; already saved: %d; excluded: %d' %
                   (pending, resumed, skipped), file=self.file)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.transfer.close()
        for label in reversed(self.labels):
            label.close()
        self.overall.close()

    def counts(self):
        self.overall.set_postfix(downloaded=self.downloaded, failed=self.failed,
                                 saved=self.resumed, excluded=self.skipped)

    def start(self, entry, url, mode):
        self.entry = entry
        width = max(30, shutil.get_terminal_size().columns - 1)
        values = ('Title: ' + entry.get('title', entry['ID']),
                  'DOI: ' + entry.get('doi', '(missing)'), 'URL: ' + url)
        for label, value in zip(self.labels, values):
            value = ' '.join(value.split())
            label.set_description_str(value if len(value) <= width else value[:width-3] + '...')
        self.transfer.total = None
        self.transfer.reset()
        self.transfer.set_description_str(mode)

    def status(self, value):
        self.transfer.set_description_str(value)

    def total_bytes(self, value):
        try:
            total = int(value)
        except (ValueError, TypeError):
            total = None
        self.transfer.total = total if total is not None and total > 0 else None
        self.transfer.refresh()

    def advance(self, count):
        self.transfer.update(count)

    def finished(self, success):
        if success:
            self.downloaded += 1
        else:
            self.failed += 1
        self.overall.update(1)
        self.counts()

    def browser_url(self, entry, url):
        self.start(entry, url, 'Browser (waiting for file)')
