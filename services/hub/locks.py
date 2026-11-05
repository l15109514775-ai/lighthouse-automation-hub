"""Reuse the existing worker's Windows/Linux OS lock without loading its models."""
from contextlib import ExitStack, contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from .errors import HubError

_path = Path(__file__).resolve().parents[2] / 'vendor/subtitle-cloud-worker/observer/store.py'
_spec = spec_from_file_location('lighthouse_upstream_store', _path)
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
exclusive = _module.exclusive


@contextmanager
def resource_guard(*paths):
    with ExitStack() as stack:
        try:
            for path in paths:
                stack.enter_context(exclusive(path))
        except OSError:
            raise HubError('HEAVY_RESOURCE_BUSY', retryable=True) from None
        yield
