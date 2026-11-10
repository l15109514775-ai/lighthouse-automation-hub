"""Read-only health probe: both producer and worker must belong to this boot."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time
import re
import uuid

MARKER = Path('/tmp/replay-acquisition-epoch.json')
NAMES = ('worker:replay-acquisition', 'worker:replay-acquisition-producer')


def instance_names(instance):
    if not isinstance(instance, str) or not re.fullmatch('[a-f0-9]{32}', instance):
        raise ValueError('ACQUISITION_HEALTH_INSTANCE_INVALID')
    return tuple(name + ':' + instance for name in NAMES)


def start_epoch(path=MARKER):
    # /tmp is the container's private tmpfs, not a shared host directory.
    instance = uuid.uuid4().hex
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'started_at': time.time(), 'instance': instance}, stream)
    path.chmod(0o600)
    return instance


def healthy(database, marker=MARKER, *, now=None, max_age=150):
    now = time.time() if now is None else now
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            return False
        epoch = json.loads(marker.read_text('utf-8'))
        started, names = epoch['started_at'], instance_names(epoch['instance'])
        if not isinstance(started, (float, int)) or not 0 < started <= now:
            return False
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            rows = dict(db.execute('SELECT name,at FROM heartbeats WHERE name IN (?,?)', names))
        return all(name in rows and started <= rows[name] <= now and now - rows[name] <= max_age for name in names)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


if __name__ == '__main__':
    root = Path(os.environ.get('HUB_ROOT', '/opt/lighthouse-hub'))
    raise SystemExit(0 if healthy(root / 'data/hub.sqlite3') else 1)
