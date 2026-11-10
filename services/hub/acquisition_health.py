"""Read-only health probe: both producer and worker must belong to this boot."""
import json
import os
from pathlib import Path
import sqlite3
import time

MARKER = Path('/tmp/replay-acquisition-epoch.json')
NAMES = ('worker:replay-acquisition', 'worker:replay-acquisition-producer')


def start_epoch(path=MARKER):
    # /tmp is the container's private tmpfs, not a shared host directory.
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'started_at': time.time()}, stream)
    path.chmod(0o600)


def healthy(database, marker=MARKER, *, now=None, max_age=150):
    now = time.time() if now is None else now
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            return False
        started = json.loads(marker.read_text('utf-8'))['started_at']
        if not isinstance(started, (float, int)) or not 0 < started <= now:
            return False
        with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            rows = dict(db.execute('SELECT name,at FROM heartbeats WHERE name IN (?,?)', NAMES))
        return all(name in rows and started <= rows[name] <= now and now - rows[name] <= max_age for name in NAMES)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


if __name__ == '__main__':
    root = Path(os.environ.get('HUB_ROOT', '/opt/lighthouse-hub'))
    raise SystemExit(0 if healthy(root / 'data/hub.sqlite3') else 1)
