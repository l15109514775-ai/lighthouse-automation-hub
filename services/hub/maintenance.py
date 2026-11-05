from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import time
from .errors import HubError


def backup(store, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.partial')
    source = store.connect()
    target = sqlite3.connect(temp)
    try:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise HubError('BACKUP_INTEGRITY_FAILED')
        target.commit()
    finally:
        target.close()
        source.close()
    os.replace(temp, destination)
    digest = hashlib.file_digest(destination.open('rb'), 'sha256').hexdigest()
    destination.with_suffix('.sha256').write_text(digest + '\n', 'ascii')
    return {'file': str(destination), 'sha256': digest, 'restore_verified': True}


class Maintenance:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def check_disk(self, reserve_bytes=0):
        free = shutil.disk_usage(self.settings.root).free
        floor = int(self.settings.config['disk_floor_gb'] * 1_000_000_000)
        if free - reserve_bytes < floor:
            self.store.event('disk.low', {'free_bytes': free, 'floor_bytes': floor}, dedup_key='disk.low')
            raise HubError('DISK_LOW', review=True)
        return free

    def monitor(self, job):
        try:
            self.check_disk()
        except HubError:
            pass
        now = time.time()
        from .monitoring import host_snapshot
        snapshot=host_snapshot(self.settings)
        if snapshot.get('available'):
            for container in snapshot['containers']:
                if container.get('state')!='running' or container.get('health')=='unhealthy':
                    self.store.event('pipeline.stalled',{'queue':'system','service':container.get('name'),
                        'code':'CONTAINER_UNHEALTHY'},dedup_key='container:' + str(container.get('name')))
        for row in self.store.query('''SELECT j.queue,MIN(j.created_at) oldest FROM jobs j JOIN pipelines p ON p.name=j.queue
              WHERE j.state IN ('pending','retry_scheduled') AND p.paused=0 GROUP BY j.queue'''):
            if now - row['oldest'] > 7200:
                self.store.event('pipeline.stalled', {'queue': row['queue']}, dedup_key='stalled:' + row['queue'])
        from .events import Events
        return {'monitor': 'checked', 'events':Events(self.settings, self.store).dispatch()}

    def backup_job(self, job):
        name = datetime.now(timezone.utc).strftime('hub-%Y%m%dT%H%M%S-') + job['job_id'][:8] + '.sqlite3'
        result = backup(self.store, self.settings.root / 'backups' / name)
        result['external_backup'] = 'not_configured' if not os.getenv('BACKUP_EXTERNAL_DESTINATION') else 'adapter_not_connected'
        return result

    def cleanup(self, job):
        # Only disposable .partial files under terminal jobs may be removed.
        # Originals, source archives and unconfirmed/recoverable media are never deleted.
        removed = 0
        cutoff = time.time() - self.settings.config['temporary_retention_days'] * 86400
        root = (self.settings.root / 'media').resolve()
        for row in self.store.query("SELECT job_id FROM jobs WHERE state='completed' AND updated_at<?", (cutoff,)):
            folder = root / row['job_id']
            if not folder.is_dir() or folder.is_symlink():
                continue
            for file in folder.glob('*.partial'):
                if file.is_symlink() or not file.resolve().is_relative_to(root):
                    continue
                if file.stat().st_mtime < cutoff:
                    file.unlink()
                    removed += 1
        return {'disposable_partials_removed': removed, 'original_media_removed': 0}
