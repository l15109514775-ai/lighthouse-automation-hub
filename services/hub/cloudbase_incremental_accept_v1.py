"""Chain-aware acceptance for the incremental CloudBase backup.

The caller supplies a bounded `fetch(ref)` callback. This module owns the
ordering, byte reservation, restore, and database checks; it never decides how
to authenticate or which URLs to use.
"""
import sqlite3
from pathlib import Path

from .cloudbase_incremental_v1 import IncrementalError, chain_budget, restore_from_remote

VERSION = 'cloudbase-incremental-acceptance-v1'


def accept(refs, fetch, destination, *, limits=None):
    if not refs:
        raise IncrementalError('INCREMENTAL_CHAIN_EMPTY')
    budget = chain_budget(refs)
    if limits is not None and budget['archive_bytes'] > limits:
        raise IncrementalError('INCREMENTAL_CHAIN_BUDGET_EXCEEDED')
    head = refs[-1].get('manifest')
    if not isinstance(head, dict) or head.get('version') != 'cloudbase-operational-backup-v1':
        raise IncrementalError('INCREMENTAL_HEAD_MANIFEST_INVALID')
    result = restore_from_remote(refs, fetch, destination)
    database = Path(destination) / 'database/credential-excluded-operational.sqlite3'
    if not database.is_file():
        raise IncrementalError('INCREMENTAL_DATABASE_MISSING')
    db = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        foreign = db.execute('PRAGMA foreign_key_check').fetchall()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        excluded = head.get('snapshot', {}).get('excluded_tables', {})
        for table in excluded:
            if table not in tables:
                raise IncrementalError('INCREMENTAL_EXCLUDED_TABLE_MISSING')
            if db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] != 0:
                raise IncrementalError('INCREMENTAL_EXCLUDED_TABLE_NOT_EMPTY')
        counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('jobs', 'publications', 'events', 'audit') if table in tables}
    finally:
        db.close()
    if integrity != 'ok' or foreign:
        raise IncrementalError('INCREMENTAL_DATABASE_INVALID')
    return {'version': VERSION, 'state': 'chain_restored_and_verified',
            'chain': [ref.get('operation_id') for ref in refs],
            'entries': result['entries'], 'archive_bytes': budget['archive_bytes'],
            'archive_requests': budget['requests'], 'sqlite_integrity': integrity,
            'sqlite_foreign_keys': foreign, 'restored_counts': counts,
            'additional_remote_gets': budget['requests']}
