"""Compose the 30-day planner with a full-archive builder.

The upload/acceptance layer supplies the full builder and only calls this module
after the prior archive has been verified.  No network or deletion lives here.
"""
from pathlib import Path

from .cloudbase_incremental_v1 import BASE, DELTA, FileIndex, build_delta

VERSION = 'cloudbase-incremental-runner-v1'


class IncrementalBackup:
    def __init__(self, index, full_builder):
        if not isinstance(index, FileIndex):
            raise ValueError('INCREMENTAL_INDEX_REQUIRED')
        self.index, self.full_builder = index, full_builder

    def prepare(self, operation_id, *, now, base_days=30):
        plan = self.index.plan(now=now, base_days=base_days)
        full = self.full_builder(operation_id)
        # The builder contract is intentionally small: an archive path and its
        # already validated full operational manifest.
        archive, manifest = Path(full['archive']), full['manifest']
        if plan['kind'] == BASE:
            return {'version': VERSION, 'kind': BASE, 'plan': plan,
                    'archive': str(archive), 'bytes': archive.stat().st_size,
                    'manifest': manifest, 'base': None}
        delta = build_delta(archive, manifest, self.index, operation_id=operation_id,
                            base=plan['base'], output=archive.with_name('incremental-delta.tar.gz'))
        return {'version': VERSION, 'kind': DELTA, 'plan': plan, **delta,
                'full_archive': str(archive)}

    def commit(self, bundle, *, archive_key, archive_sha256, manifest_sha256):
        manifest = bundle['manifest']
        kind = bundle['kind']
        base_operation_id = (manifest.get('base') or {}).get('operation_id')
        self.index.record(manifest, operation_id=manifest['operation_id'],
                          archive_key=archive_key, archive_sha256=archive_sha256,
                          manifest_sha256=manifest_sha256, kind=kind,
                          base_operation_id=base_operation_id,
                          archive_bytes=bundle.get('bytes', 0))
        return {'state': 'indexed', 'kind': kind, 'operation_id': manifest['operation_id']}
