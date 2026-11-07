"""Credential-free, read-only operational projection for a separate MCP observer.

Uses the live SQLite WAL snapshot, never app initialization or provider clients.
Only the explicitly named output is written; no original job/file is modified.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
import uuid

VERSION = 'lighthouse-observer-snapshot-v1'
MAX_JOBS, MAX_REPLAYS, MAX_JSON = 250, 50, 2_097_152
FIELDS = ('job_id,source_platform,source_id,job_type,queue,state,stage,attempts,'
          'error_code,created_at,updated_at,external_publish_id,policy_version')
BAD_STATES = {'failed', 'awaiting_review', 'auth_expired', 'publish_unknown'}
# Source identity reconciliation is a normal wait for a paused/late-finalized
# replay: YouTube rewrites duration/definition after the plan snapshot. It must
# not be projected as a failure; it is surfaced separately as waiting.
NORMAL_WAIT_ERRORS = {'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED'}


def is_normal_wait(row):
    return (row.get('state') == 'awaiting_review' and
            row.get('error_code') in NORMAL_WAIT_ERRORS and
            (row.get('queue') == 'video' or str(row.get('job_type', '')).startswith('youtube_replay_')))
BEATS = ('scheduler', 'worker:tweet', 'worker:video', 'worker:system')
PERIODIC = {'x_discovery', 'youtube_discovery', 'youtube_feed', 'youtube_live_monitor',
            'maintenance', 'backup', 'cleanup', 'bilibili_token_check', 'tweet_machine_reconcile',
            'tweet_web_reconcile', 'bilibili_reconcile'}
TRANSLATION_SOURCE = re.compile(
    r'^(?P<parent>[a-f0-9-]{36}):(?P<role>[A-Za-z0-9_.-]{1,64})(?::revision-(?P<index>[1-9][0-9]{0,3}))?$')
CN = timezone(timedelta(hours=8), 'Asia/Shanghai')


def number(value, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def token(value, maximum=160):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,' + str(maximum) + '}', value) else None


def safe_path(path):
    path = Path(path).absolute()
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('OBSERVER_UNSAFE_PATH')
    return path


def read_json(path, maximum=MAX_JSON, *, root_owned=False):
    path = safe_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum or
                root_owned and os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022)):
            raise ValueError('OBSERVER_INVALID_DOCUMENT')
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('OBSERVER_DOCUMENT_TOO_LARGE')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('OBSERVER_DOCUMENT_NOT_OBJECT')
    return value


@contextmanager
def readonly_database(path):
    path = safe_path(path)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def source_url(platform, ident):
    if platform == 'youtube' and re.fullmatch(r'[A-Za-z0-9_-]{11}', ident or ''):
        return 'https://www.youtube.com/watch?v=' + ident
    if platform in ('x', 'machine_web') and re.fullmatch(r'[0-9]{5,25}', ident or ''):
        return 'https://x.com/i/status/' + ident
    return None


def public_url(row):
    if row['platform'] != 'bilibili' or row['state'] != 'completed':
        return None
    ident = row['external_id'] or ''
    expected = (['https://www.bilibili.com/video/' + ident, 'https://www.bilibili.com/video/' + ident + '/']
                if re.fullmatch(r'BV[A-Za-z0-9]{10}', ident) else
                ['https://t.bilibili.com/' + ident, 'https://www.bilibili.com/opus/' + ident]
                if re.fullmatch(r'[0-9]{5,25}', ident) else [])
    return row['external_url'] if row['external_url'] in expected else None


def translation_resolution(db, row, parent_publications):
    """Bind a failed base translation node to its single revision and parent publication.

    The V5 machine policy admits at most one revision-1 retranslation. Only an
    exact source-id pattern qualifies; a revision completed together with a
    completed parent and a completed publication makes the base node history.
    Nothing here changes database rows or claims anything not recorded.
    """
    if row['job_type'] != 'machine_translation_node' or row['queue'] != 'tweet':
        return None
    match = TRANSLATION_SOURCE.fullmatch(row['source_id'] or '')
    if not match or match.group('index') is not None:
        return None
    parent, role = match.group('parent'), match.group('role')
    revision = db.execute(
        'SELECT job_id,state,stage,attempts,error_code,updated_at FROM jobs WHERE source_id=?',
        (parent + ':' + role + ':revision-1',)).fetchone()
    if revision is None:
        return None
    if (str(uuid.UUID(revision['job_id'])) != revision['job_id'] or
            str(uuid.UUID(parent)) != parent or not number(revision['updated_at'])):
        return None
    superseded = revision['state'] == 'completed'
    parent_row = db.execute('SELECT state,stage FROM jobs WHERE job_id=?', (parent,)).fetchone()
    parent_state = parent_row['state'] if parent_row else None
    published = parent_publications.get(parent)
    complete = bool(superseded and parent_state == 'completed' and published)
    return {'kind': 'superseded_by_revision',
            'revision_job_id': revision['job_id'],
            'revision_state': revision['state'],
            'revision_stage': revision['stage'],
            'revision_error_code': revision['error_code'],
            'revision_updated_at': revision['updated_at'],
            'parent_job_id': parent,
            'parent_state': parent_state,
            'published_url': published if complete else None,
            'supersession_complete': complete}


def progress(root, db, job, now):
    value = {'phase': token(job['stage']) or 'unknown', 'downloaded_bytes': None,
             'total_bytes': None, 'percent': None, 'observed_at': None, 'fresh': False,
             'format_id': None, 'percent_scope': None, 'reason': 'no_measured_progress'}
    if not job['is_current'] or job['is_historical']:
        value['reason'] = 'historical_job'; return value
    if job['job_type'] != 'youtube_replay_download_v4' or job['state'] != 'processing':
        return value  # Upload stages have no measured native percentage.
    try:
        ident = str(uuid.UUID(job['job_id']))
        if ident != job['job_id']: raise ValueError()
        row = db.execute('SELECT substr(result,1,?) result FROM jobs WHERE job_id=?', (MAX_JSON + 1, ident)).fetchone()
        if len(row['result'].encode()) > MAX_JSON: raise ValueError()
        state = json.loads(row['result'])['youtube_download_v4']
        binding = state['binding']
        if (state['state'] != 'acquiring' or binding['job_id'] != ident or
                binding['video_id'] != job['source_id']): raise ValueError()
        data = read_json(root / 'media' / ident / 'youtube-whole-v4/progress.json')
        if data.get('version') != 'youtube-whole-replay-download-v4' or data.get('source_plan') != binding['source_plan']:
            raise ValueError()
        at = data.get('checked_at')
        if not number(at) or not 0 <= now - at <= 120:
            value['reason'] = 'stale_progress'; return value
        selected = [f for f in binding['source_plan']['selected_formats'] if f['format_id'] == data.get('format_id')]
        done, total = data.get('downloaded_bytes'), data.get('total_bytes')
        if (len(selected) != 1 or type(done) is not int or type(total) is not int or not 0 <= done <= total or
                not 0 < total <= 2**53 or total != selected[0]['filesize'] or data.get('status') not in ('downloading', 'finished')):
            raise ValueError()
        if not token(data['format_id'], 24): raise ValueError()
        value.update(phase='download', format_id=data['format_id'], percent_scope='current_track',
                     downloaded_bytes=done, total_bytes=total,
                     percent=round(done * 100 / total, 2), observed_at=at, fresh=True, reason=None)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        value['reason'] = 'progress_unavailable_or_mismatched'
    return value


def backup_status(path, now):
    value = {'configured': None, 'status': 'unknown', 'last_verified_at': None,
             'last_verified_age_seconds': None, 'last_error_at': None, 'error_code': None, 'reason': 'not_integrated'}
    if path is None: return value
    try:
        body = read_json(path)
        good = (body.get('latest_verified') or {}).get('updated_at')
        bad = body.get('last_error') or {}; at = bad.get('updated_at')
        if good is not None and (not number(good) or good > now): raise ValueError()
        if at is not None and (not number(at) or at > now): raise ValueError()
        code = bad.get('error_code')
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', code)): raise ValueError()
        value.update(configured=True, last_verified_at=good, last_verified_age_seconds=now-good if good is not None else None,
                     last_error_at=at, error_code=code, reason=None,
                     status='last_run_failed' if at is not None and (good is None or at > good) else 'verified' if good is not None else 'no_verified_backup')
    except (OSError, ValueError, TypeError, AttributeError): value['reason'] = 'backup_status_unavailable'
    return value


def container_status(path, now):
    empty = {'available': False, 'checked_at': None, 'items': [], 'reason': 'snapshot_unavailable'}
    try:
        data = read_json(path, 100_000, root_owned=True); at = data.get('checked_at')
        if not number(at) or not 0 <= now-at <= 180: return {**empty, 'reason': 'stale_snapshot'}
        rows = data['containers']
        if not isinstance(rows, list) or len(rows) > 20: raise ValueError()
        items = []
        for row in rows:
            if (not token(row.get('name'), 80) or row.get('state') not in ('running','exited','created','paused','restarting','dead','removing') or
                    row.get('health') not in (None,'healthy','unhealthy','starting','none','not_configured') or
                    type(row.get('exit_code')) is not int or type(row.get('restart_count')) is not int or row['restart_count'] < 0): raise ValueError()
            items.append({k: row[k] for k in ('name','state','health','exit_code','restart_count')})
        return {'available': True, 'checked_at': at, 'items': items, 'reason': None}
    except (OSError, ValueError, TypeError, KeyError): return empty


def system_usage(root):
    disk = shutil.disk_usage(root); mem = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            name, value = line.split(':', 1)
            if name in ('MemTotal','MemAvailable'): mem[name] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError): pass
    try: load = os.getloadavg()[0]
    except (AttributeError, OSError): load = None
    return {'disk_free_bytes': disk.free, 'disk_total_bytes': disk.total, 'memory_total_bytes': mem.get('MemTotal'),
            'memory_available_bytes': mem.get('MemAvailable'), 'load_1m': load}


def current_repost_checks(db):
    """Follow recorded submission/publication bindings, never infer from a task name alone."""
    base, replacement = 'tweet-native-repost-check-v1', 'tweet-native-repost-reinspection-v2'
    query = ('SELECT job_id,source_id,policy_version,substr(payload,1,?) request FROM jobs '
             "WHERE job_type='tweet_repost_check_v1' AND source_platform='bilibili' AND queue='tweet' ")

    def binding(row):
        if row is None or row['policy_version'] not in (base,replacement): return None
        try:
            if len(row['request'].encode()) > MAX_JSON: return None
            body = json.loads(row['request']); external = body['external_id']; parent = body['submission_job_id']
            if (body['version'] != base or external != row['source_id'] or not re.fullmatch(r'[0-9]{5,25}',external) or
                    str(uuid.UUID(parent)) != parent or body['target_site_uid'] != '1000000000000000' or
                    not re.fullmatch(r'[0-9]{5,25}',body['source_id']) or not token(body['publication_kind']) or
                    not isinstance(body['publication_key'],str) or not 0<len(body['publication_key'])<=256 or
                    row['policy_version'] == base and 'reinspection' in body): return None
            found = db.execute("""SELECT 1 FROM jobs j JOIN publications p ON p.job_id=j.job_id
                WHERE j.job_id=? AND j.source_platform='x' AND j.job_type='tweet_process' AND j.queue='tweet'
                AND j.state='completed' AND j.source_id=? AND j.external_publish_id=?
                AND p.idempotency_key=? AND p.platform='bilibili' AND p.state='completed'
                AND p.source_platform='x' AND p.source_id=? AND p.external_id=? AND p.kind=?
                AND p.account_id='site_uid:1000000000000000'""",
                (parent,body['source_id'],external,body['publication_key'],body['source_id'],external,body['publication_kind'])).fetchone()
            return (parent,body['publication_key'],external,body) if found else None
        except (ValueError,TypeError,KeyError,AttributeError): return None

    rows = list(db.execute(query+'ORDER BY created_at DESC LIMIT 100',(MAX_JSON+1,)))
    current, superseded = set(), set()
    for row in rows:
        info = binding(row)
        if info is None: continue
        if row['policy_version'] == replacement:
            recheck = info[3].get('reinspection')
            if not isinstance(recheck,dict): continue
            previous_id = recheck.get('previous_check_job_id')
            if not isinstance(previous_id,str) or not re.fullmatch(r'[a-f0-9-]{36}',previous_id): continue
            previous = db.execute(query+"AND job_id=? AND policy_version=?",(MAX_JSON+1,previous_id,base)).fetchone()
            old = binding(previous)
            if (old is None or info[:3] != old[:3] or recheck.get('version') != replacement or
                    {k:v for k,v in info[3].items() if k!='reinspection'} != old[3]): continue
            superseded.add(previous_id)
        current.add(row['job_id'])
    return current-superseded


def collect_snapshot(root, database, *, now=None, backup=None, active_tweet_policy='machine-tweet-v4-api-relations'):
    root = safe_path(root); now = time.time() if now is None else now
    if not number(now): raise ValueError('OBSERVER_INVALID_TIME')
    with readonly_database(database) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'jobs','pipelines','heartbeats'} <= tables: raise ValueError('OBSERVER_REQUIRED_TABLES_MISSING')
        fifo = [dict(r) for r in db.execute("SELECT candidate_id,source_id,state,reason,plan_job_id,download_job_id,publication_job_id,refresh_job_id,updated_at FROM replay_fifo_v4 WHERE state NOT IN ('excluded','existing') ORDER BY (state='completed'),updated_at DESC LIMIT ?", (MAX_REPLAYS,))] if 'replay_fifo_v4' in tables else []
        references = {r[k] for r in fifo for k in ('plan_job_id','download_job_id','publication_job_id','refresh_job_id') if r[k]}
        parent_ids = [r['publication_job_id'] for r in fifo if r['publication_job_id']]
        children = []
        if parent_ids and 'replay_part_children_v5' in tables:
            children = [dict(r) for r in db.execute('SELECT parent_job_id,child_job_id FROM replay_part_children_v5 WHERE parent_job_id IN ('+','.join('?'*len(parent_ids))+') ORDER BY parent_job_id,part_index LIMIT 100', parent_ids)]
            references.update(r['child_job_id'] for r in children)
        if references and 'replay_part_lookup_schedule_v5' in tables:
            current_parents=sorted(references)
            references.update(r[0] for r in db.execute('SELECT active_child_id FROM replay_part_lookup_schedule_v5 WHERE active_child_id IS NOT NULL AND parent_job_id IN ('+','.join('?'*len(current_parents))+') LIMIT 100',current_parents))
        if 'publications' in tables: references.update(current_repost_checks(db))
        records = [dict(r) for r in db.execute('SELECT '+FIELDS+' FROM jobs ORDER BY created_at DESC LIMIT 100')]
        if references:
            records += [dict(r) for r in db.execute('SELECT '+FIELDS+' FROM jobs WHERE job_id IN ('+','.join('?'*len(references))+')', sorted(references))]
        records = list({r['job_id']: r for r in records}.values())
        records.sort(key=lambda r: (r['job_id'] not in references, -r['created_at']))
        records = records[:MAX_JOBS]; jobs = []
        latest_periodic = {}
        for row in sorted(records, key=lambda r: -r['created_at']): latest_periodic.setdefault(row['job_type'], row['job_id'])
        publications = []
        if records and 'publications' in tables:
            publications = [dict(r) for r in db.execute('SELECT job_id,platform,state,external_id,external_url FROM publications WHERE job_id IN ('+','.join('?'*len(records))+') LIMIT 300', [r['job_id'] for r in records])]
        links = {r['job_id']: public_url(r) for r in publications if public_url(r)}
        translation_parents = sorted({m.group('parent') for row in records
            if (m := TRANSLATION_SOURCE.fullmatch(row['source_id'] or '')) and row['job_type'] == 'machine_translation_node'})
        parent_publications = {}
        if translation_parents and 'publications' in tables:
            parent_publications = {r['job_id']: public_url(r) for r in
                db.execute('SELECT job_id,platform,state,external_id,external_url FROM publications WHERE job_id IN ('+','.join('?'*len(translation_parents))+') LIMIT 200', translation_parents)
                if public_url(r)}
        for row in records:
            historical = '_history_' in row['job_type'] or row['job_type'] == 'video_process'
            current = (row['job_id'] in references or row['queue'] != 'video' and row['policy_version'] == active_tweet_policy or
                row['job_type'] in PERIODIC and latest_periodic[row['job_type']] == row['job_id'])
            current = bool(current and not historical)
            item = {k: row[k] for k in FIELDS.split(',') if k != 'policy_version'}
            for key in ('source_platform','source_id','job_type','queue','state','stage','external_publish_id'): item[key] = token(item[key])
            item['error_code'] = row['error_code'] if isinstance(row['error_code'], str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', row['error_code']) else None
            resolution = translation_resolution(db, row, parent_publications)
            if resolution is not None:
                resolution = {k: (token(v) if k in ('revision_stage', 'revision_error_code') and isinstance(v, str) else v)
                              for k, v in resolution.items()}
            item.update(is_historical=historical or not current, is_current=current,
                        source_url=source_url(row['source_platform'],row['source_id']), published_url=links.get(row['job_id']),
                        resolution=resolution, normal_wait=is_normal_wait(row),
                        attention='waiting_source_reconciliation' if is_normal_wait(row) else
                                  ('error' if row['state'] in BAD_STATES else None))
            item['progress'] = progress(root, db, item, now); jobs.append(item)
        replay_items = []
        for row in fifo:
            item = {k: token(v) if isinstance(v,str) else v for k,v in row.items()}
            title = None
            if 'youtube_videos' in tables:
                found = db.execute('SELECT title FROM youtube_videos WHERE video_id=?', (row['source_id'],)).fetchone()
                if found and isinstance(found[0],str): title = found[0][:512]
            related = {row['publication_job_id']} | {r['child_job_id'] for r in children if r['parent_job_id']==row['publication_job_id']}
            item.update(title=title, source_url=source_url('youtube',row['source_id']), published_urls=sorted({links[k] for k in related if k in links}))
            replay_items.append(item)
        beats = {r['name']: r['at'] for r in db.execute('SELECT name,at FROM heartbeats')}
        stale = [name for name in BEATS if not number(beats.get(name)) or not 0<=now-beats[name]<=120]
        pipelines = [{'name': r['name'], 'paused': bool(r['paused'])} for r in db.execute("SELECT name,paused FROM pipelines WHERE name IN ('tweet','video','system') ORDER BY name")]
        local = datetime.fromtimestamp(now,CN); day=local.replace(hour=0,minute=0,second=0,microsecond=0); month=day.replace(day=1)
        counts = [{'state':state,'count':count} for state,count in sorted(Counter(j['state'] for j in jobs if j['is_current'] and j['updated_at']>=day.timestamp()).items())]
        usage = {name:[dict(r) for r in db.execute('SELECT provider,COUNT(*) calls,SUM(units) quota_units,SUM(cost_usd) reserved_upper_bound_usd FROM api_calls WHERE started_at>=? GROUP BY provider LIMIT 30', (start.timestamp(),))] if 'api_calls' in tables else [] for name,start in (('daily',day),('monthly',month))}
        for rows in usage.values():
            for row in rows: row['provider'] = token(row['provider'], 50)
    return {'version': VERSION, 'generated_at': now, 'read_only': True, 'provider_requests': 0,
            'health': {'database':'ok','status':'degraded' if stale else 'ready','stale_services':stale},
            'system': system_usage(root), 'containers': container_status(root/'data/host-status.json',now),
            'pipelines': pipelines, 'jobs':jobs,'replays':replay_items,
            'failures':[j for j in jobs if j['is_current'] and j['state'] in BAD_STATES
                        and not j.get('normal_wait')
                        and not (j.get('resolution') or {}).get('supersession_complete')],
            'waiting':[j for j in jobs if j['is_current'] and j.get('normal_wait')],
            'daily_report':{'date':local.date().isoformat(),'timezone':'Asia/Shanghai','job_counts':counts,
                'job_counts_scope':'current_jobs_in_bounded_snapshot',
                'api_usage':usage,'estimated':True,'charges_confirmed':False}, 'backup':backup_status(backup,now)}


def write_snapshot(output, snapshot):
    output = safe_path(output); parent = safe_path(output.parent)
    if not parent.is_dir(): raise ValueError('OBSERVER_OUTPUT_PARENT_REQUIRED')
    raw = json.dumps(snapshot,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>MAX_JSON: raise ValueError('OBSERVER_SNAPSHOT_TOO_LARGE')
    temporary = parent / ('.observer-'+uuid.uuid4().hex+'.tmp')
    try:
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o640),'wb') as stream:
            if os.name=='posix': os.fchmod(stream.fileno(),0o640)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,output)
        if os.name=='posix':
            fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        if temporary.exists(): temporary.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','database','output'): parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--backup-status',type=Path);parser.add_argument('--active-tweet-policy',default='machine-tweet-v4-api-relations')
    args=parser.parse_args()
    try:
        snapshot=collect_snapshot(args.root,args.database,backup=args.backup_status,active_tweet_policy=args.active_tweet_policy)
        write_snapshot(args.output,snapshot)
        print(json.dumps({'version':VERSION,'written':True,'generated_at':snapshot['generated_at'],'provider_requests':0}))
    except (OSError,ValueError,TypeError,sqlite3.Error,KeyError):
        print(json.dumps({'error_code':'OBSERVER_COLLECTION_FAILED'}),file=sys.stderr);raise SystemExit(1) from None


if __name__=='__main__':main()
