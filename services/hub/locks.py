"""Reuse the existing worker's Windows/Linux OS lock without loading its models."""
from contextlib import ExitStack, contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import threading
from .errors import HubError

_path = Path(__file__).resolve().parents[2] / 'vendor/subtitle-cloud-worker/observer/store.py'
_spec = spec_from_file_location('lighthouse_upstream_store', _path)
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
_owned = threading.local()


@contextmanager
def exclusive(path):
    """Use the existing OS lock and record ownership for forced recovery.

    The proof is thread/process local; a sibling thread or fork cannot borrow it.
    The lockfile is never deleted (which would create two independent inodes).
    """
    key = (os.getpid(), str(Path(path).resolve()))
    with _module.exclusive(path):
        owned = getattr(_owned, 'paths', None)
        if owned is None:
            owned = _owned.paths = set()
        owned.add(key)
        try:
            yield
        finally:
            owned.discard(key)


def require_queue_lock(store, queue):
    if queue not in ('tweet', 'video', 'system', 'moderation'):
        raise RuntimeError('QUEUE_LOCK_SCOPE_INVALID')
    key = (os.getpid(), str((Path(store.path).parent / 'locks' / queue).resolve()))
    if key not in getattr(_owned, 'paths', set()):
        raise RuntimeError('QUEUE_LOCK_REQUIRED')


@contextmanager
def resource_guard(*paths):
    with ExitStack() as stack:
        try:
            for path in paths:
                stack.enter_context(exclusive(path))
        except OSError:
            raise HubError('HEAVY_RESOURCE_BUSY', retryable=True) from None
        yield
