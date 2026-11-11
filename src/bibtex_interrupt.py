"""Save the current BibTeX result when a command is interrupted."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from itertools import count
from pathlib import Path
import signal
import sys
from threading import current_thread, main_thread

from bibtex_io import new_database, write_bibtex


_active = ContextVar('bibtex_checkpoint', default=None)
_SIGNALS = tuple(getattr(signal, name) for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT')
                 if hasattr(signal, name))


class BibtexInterrupted(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


class Checkpoint:
    def __init__(self, output):
        self.output = Path(output)
        self.snapshot = new_database
        self.signum = None

    def interrupt(self, signum, frame):
        if self.signum is None:
            self.signum = signum
            raise BibtexInterrupted(signum)

    def save(self):
        database = self.snapshot()
        stem, extension = self.output.stem, self.output.suffix or '.bib'
        for number in count(1):
            numbered = '' if number == 1 else f'-{number}'
            destination = self.output.with_name(f'{stem}{numbered}-incomplete{extension}')
            try:
                # Reserve the name without replacing an input or earlier checkpoint.
                with destination.open('x', encoding='utf-8'):
                    pass
            except FileExistsError:
                continue
            try:
                write_bibtex(database, destination)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            return destination, len(database.entries)


def track_bibtex(snapshot):
    """Register a callable that returns the current set of completed entries."""
    checkpoint = _active.get()
    if checkpoint is not None:
        checkpoint.snapshot = snapshot


@contextmanager
def bibtex_checkpoint(output):
    """Save to the outer command's destination, even when a stage uses a temp file."""
    parent = _active.get()
    if parent is not None:
        try:
            yield
        except BaseException:
            if parent.signum is not None:
                raise BibtexInterrupted(parent.signum) from None
            raise
        return

    checkpoint = Checkpoint(output)
    token = _active.set(checkpoint)
    previous = {}
    try:
        if current_thread() is main_thread():
            for signum in _SIGNALS:
                previous[signum] = signal.signal(signum, checkpoint.interrupt)
        yield
    except BaseException as error:
        if checkpoint.signum is None and not isinstance(error, KeyboardInterrupt):
            raise
        checkpoint.signum = checkpoint.signum or signal.SIGINT
        try:
            destination, size = checkpoint.save()
            print(f'Interrupted; saved {size} processed entries to {destination}.', file=sys.stderr)
        except Exception as save_error:
            print(f'Interrupted; could not save incomplete BibTeX: {save_error}', file=sys.stderr)
        raise BibtexInterrupted(checkpoint.signum) from None
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _active.reset(token)


def interrupt_exit(command):
    """Report conventional signal status at the command boundary."""
    @wraps(command)
    def run(*args, **kwargs):
        try:
            return command(*args, **kwargs)
        except KeyboardInterrupt as error:
            if _active.get() is not None:
                raise
            return 128 + getattr(error, 'signum', signal.SIGINT)
    return run
