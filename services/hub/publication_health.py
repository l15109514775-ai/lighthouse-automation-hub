"""Publication health has its own boot marker and both independent heartbeats."""
import os
from pathlib import Path
from .acquisition_health import healthy as _healthy, start_epoch as _start_epoch

MARKER = Path('/tmp/replay-publication-epoch.json')
NAMES = ('worker:replay-publication', 'worker:replay-publication-producer')


def start_epoch():
    return _start_epoch(MARKER)


def healthy(database, marker=MARKER, **kwargs):
    return _healthy(database, marker, names=NAMES, **kwargs)


if __name__ == '__main__':
    root = Path(os.environ.get('HUB_ROOT', '/opt/lighthouse-hub'))
    raise SystemExit(0 if healthy(root / 'data/hub.sqlite3') else 1)
