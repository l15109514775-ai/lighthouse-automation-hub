"""Offline, read-only qualification of the effective acquisition configuration.

Run inside the candidate image as its application UID with production config/data
mounted read-only. This never initializes tables, creates jobs, downloads media,
rewrites policies or certifies rights. Policy consistency is not release approval.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import tomllib

from .config import Settings
from .errors import HubError
from .replay_fifo_v4 import ReplayFIFOV4, POLICY_FILE as FIFO_FILE
from .replay_multipart_v4 import validate_planning_policy, digest
from .youtube_download import CHANNELS
from .youtube_download_v4 import YouTubeWholeReplayPrepare, POLICY_FILE, PLAN_POLICY_FILE


def _read_policy(path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('POLICY_PATH_UNSAFE')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size < 16384:
        raise ValueError('POLICY_FILE_INVALID')
    if os.name == 'posix' and info.st_mode & 0o022:
        raise ValueError('POLICY_WRITABLE_BY_OTHERS')
    raw = path.read_bytes()
    if len(raw) != info.st_size:
        raise ValueError('POLICY_CHANGED_DURING_READ')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('POLICY_DUPLICATE_KEY')
            value[key] = item
        return value
    value = (json.loads(raw, object_pairs_hook=unique) if path.suffix == '.json'
             else tomllib.loads(raw.decode('utf-8-sig')))
    if not isinstance(value, dict):
        raise ValueError('POLICY_FILE_INVALID')
    return value, hashlib.sha256(raw).hexdigest()


def _history(database, fifo_hash):
    path = Path(database)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('DATABASE_PATH_UNSAFE')
    # mode=ro must fail if absent; no Store.init() or schema migration here.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {'journal_mode': db.execute('PRAGMA journal_mode').fetchone()[0],
                  'jobs': [], 'fifo_bindings': [], 'fifo_table_present': 'replay_fifo_v4' in tables}
        clean = lambda s: s if isinstance(s, str) and re.fullmatch(r'[a-zA-Z0-9_:-]{1,128}', s) else 'unrecognized'
        for kind, state, count, attempts in db.execute(
                "SELECT job_type,state,count(*),sum(attempts) FROM jobs WHERE queue='video' GROUP BY job_type,state"):
            result['jobs'].append({'job_type': clean(kind), 'state': clean(state),
                                   'count': count, 'attempts_total': attempts})
        if 'replay_fifo_v4' in tables:
            for matches, started, state, count in db.execute('''SELECT policy_sha256=?,
                    (plan_job_id IS NOT NULL OR download_job_id IS NOT NULL OR publication_job_id IS NOT NULL),
                    state,count(*) FROM replay_fifo_v4 GROUP BY 1,2,3''', (fifo_hash,)):
                result['fifo_bindings'].append({'matches_current_policy': None if fifo_hash is None else bool(matches),
                                                'work_started': bool(started), 'state': clean(state), 'count': count})
        db.rollback()
        return result


def inspect(settings):
    report = {'version': 'acquisition-preflight-v2', 'read_only': True,
              'effective_uid': os.geteuid() if hasattr(os, 'geteuid') else None,
              'network_used': False, 'production_activation_allowed': False,
              'policy_files': {}, 'blockers': [], 'history': None}
    blockers = report['blockers']
    values = {}
    for name in (FIFO_FILE, POLICY_FILE, PLAN_POLICY_FILE):
        try:
            value, sha = _read_policy(settings.root / 'config' / name)
            values[name] = value
            report['policy_files'][name] = {'readable': True, 'sha256': sha}
        except (OSError, ValueError, TypeError, UnicodeError):
            report['policy_files'][name] = {'readable': False}
            blockers.append('POLICY_UNREADABLE_OR_INVALID:' + name)
    if settings.config.get('youtube_whole_replay_download_enabled') is not True:
        blockers.append('YOUTUBE_WHOLE_DISABLED')
    if settings.config.get('youtube_natural_download_enabled', False) is not False:
        blockers.append('YOUTUBE_WHOLE_LEGACY_AUTOMATION_ACTIVE')
    try:
        runtime_channels = {x['youtube_channel_id'] for x in settings.config['sources']}
        if runtime_channels != CHANNELS:
            blockers.append('RUNTIME_CHANNEL_SCOPE_MISMATCH')
        for name in (FIFO_FILE, POLICY_FILE):
            if name in values and values[name].get('allowed_channel_ids') != sorted(CHANNELS):
                blockers.append('CHANNEL_SCOPE_MISMATCH:' + name)
        planning = validate_planning_policy(values[PLAN_POLICY_FILE])
        planning_hash = digest(planning)
        report['planning_sha256'] = planning_hash
        for name in (FIFO_FILE, POLICY_FILE):
            if name in values and values[name].get('planning_policy_sha256') != planning_hash:
                blockers.append('PLANNING_BINDING_MISMATCH:' + name)
    except (HubError, KeyError, TypeError, ValueError):
        blockers.append('PLANNING_OR_RUNTIME_CONFIG_INVALID')
    # Use the actual worker's validators, not a second definition of authorization.
    for name, validator in (('fifo', ReplayFIFOV4(settings, None).policy),
                            ('acquisition', YouTubeWholeReplayPrepare(settings, None).policy)):
        try:
            validator()
        except HubError as exc:
            blockers.append(exc.code)
        except (OSError, ValueError, KeyError, TypeError):
            blockers.append('RUNTIME_POLICY_INVALID:' + name)
    fifo_hash = report['policy_files'].get(FIFO_FILE, {}).get('sha256')
    try:
        report['history'] = _history(settings.db_path, fifo_hash)
        active = [item for item in report['history']['fifo_bindings']
                  if item['state'] not in ('completed', 'excluded', 'existing')]
        if fifo_hash is None and active:
            blockers.append('HISTORICAL_FIFO_BINDINGS_UNVERIFIED')
        elif any(not item['matches_current_policy'] for item in active):
            blockers.append('HISTORICAL_FIFO_BINDINGS_REQUIRE_RECONCILIATION')
        report['historical_terminal_rows_preserved'] = sum(item['count'] for item in
            report['history']['fifo_bindings'] if item['state'] in ('completed', 'excluded', 'existing'))
    except (OSError, ValueError, sqlite3.Error):
        blockers.append('DATABASE_READONLY_INSPECTION_FAILED')
    # Catch an operator replacing policy files while this report is assembled.
    for name, evidence in report['policy_files'].items():
        if evidence['readable']:
            try:
                if _read_policy(settings.root / 'config' / name)[1] != evidence['sha256']:
                    blockers.append('POLICY_CHANGED_DURING_READ:' + name)
            except (OSError, ValueError, TypeError, UnicodeError):
                blockers.append('POLICY_CHANGED_DURING_READ:' + name)
    report['blockers'] = sorted(set(blockers))
    report['configuration_consistent'] = not report['blockers']
    return report


def main():
    try:
        report = inspect(Settings.load())
    except (OSError, ValueError, KeyError, TypeError):
        report = {'version': 'acquisition-preflight-v2', 'read_only': True,
                  'production_activation_allowed': False, 'configuration_consistent': False,
                  'blockers': ['RUNTIME_CONFIG_UNREADABLE_OR_INVALID']}
    print(json.dumps(report, sort_keys=True))
    return 0 if report['configuration_consistent'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
