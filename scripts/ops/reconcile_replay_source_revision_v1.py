"""Reconcile replay candidates held by a source revision that the identity gate rejects.

The FIFO gates duration against the registered snapshot with a five second window.
Two legitimate platform behaviours exceed it:

* the archive was registered while ``status.uploadStatus`` was ``uploaded`` and is
  finalised later (same video, channel, actual start/end; new duration and
  processing metadata);
* an already finalised archive is revised again by the platform (small shrink).

The pass is tiered. Only evidence-bound revisions are applied automatically; every
other blocked candidate gets a complete decision package (evidence, reason, exact
command) plus a hub ``review.required`` event, so a human decision costs one
command and no re-investigation. Nothing here relaxes the identity gate, deletes
rows, resets attempts, or creates downloads/publications by itself.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
import sqlite3
import tempfile
import time

from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.replay_fifo_v4 import (ASSET_JOBS, DOWNLOAD_JOB, MAX_DURATION_REVISION_SECONDS,
    PLAN_JOB, ReplayFIFOV4, observed_duration, sha, stamp)
from services.hub.replay_multipart_v4 import digest
from services.hub.youtube_download_v4 import YouTubeWholeReplayPrepare
from services.hub.youtube_readiness_v4 import WAIT, WholeReadinessCoordinatorV4

VERSION = 'reconcile-replay-source-revision-v1'
ERROR = 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED'
CRASH = 'UNEXPECTED_RUNTIME_ERROR'
MAX_RESUMES_PER_DAY = 2
AUTO_PROCESSING_MAX_SECONDS = 120
AUTO_FINALIZED_MAX_SECONDS = 60
APPROVAL_MAX_SECONDS = 600
STABLE_FINAL_HOURS = 6
STABLE_OBSERVATIONS = 3
MAX_REBASES_PER_DAY = 2
MAX_CANDIDATES = 20
STATE_FILE = 'reconcile-observations.json'
MUTABLE_PATHS = ('/contentDetails/definition', '/contentDetails/duration', '/etag',
                 '/liveStreamingDetails/activeLiveChatId', '/snippet/publishedAt',
                 '/snippet/title', '/snippet/description', '/status/uploadStatus')
MUTABLE_PREFIXES = ('/snippet/thumbnails/',)


class Skip(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def need(value, code):
    if not value:
        raise Skip(code)


def runtime_allowed():
    return os.name == 'posix' and os.getuid() == 10001


def leaf_diff(before, after, path=''):
    out = {}
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            out.update(leaf_diff(before.get(key), after.get(key), path + '/' + key))
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            left = before[index] if index < len(before) else None
            right = after[index] if index < len(after) else None
            out.update(leaf_diff(left, right, path + '/%d' % index))
    elif before != after:
        out[path] = {'before': before, 'after': after}
    return out


def mutable(path):
    return path in MUTABLE_PATHS or any(path.startswith(prefix) for prefix in MUTABLE_PREFIXES)


def row_dict(row):
    value = dict(row)
    for key in ('payload', 'result'):
        value[key] = json.loads(value[key])
    return value


def observation_signature(raw, identity):
    """Stable fingerprint of one official observation for the finalisation fallback."""
    body = {'duration': raw['contentDetails']['duration'],
            'definition': raw['contentDetails'].get('definition'),
            'etag': raw.get('etag'), 'upload_status': raw['status'].get('uploadStatus'),
            'actual_start': identity['actual_start'], 'actual_end': identity['actual_end'],
            'privacy': identity['privacy_status']}
    return hashlib.sha256(encode(body).encode()).hexdigest()


def load_state(settings):
    path = settings.root / 'data' / 'ops-maintenance' / STATE_FILE
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(settings, state, now):
    folder = settings.root / 'data' / 'ops-maintenance'
    folder.mkdir(mode=0o700, exist_ok=True)
    keep = {key: value for key, value in state.items() if now - value.get('first_at', now) <= 7 * 86400}
    path = folder / STATE_FILE
    body = encode(keep).encode()
    with open(path, 'wb') as stream:
        stream.write(body)
    os.chmod(path, 0o600)
    return keep


def observe(state, candidate_id, signature, now, final=False):
    record = state.get(candidate_id, {})
    if record.get('signature') == signature:
        record['observations'] = record.get('observations', 1) + 1
    else:
        record = {'signature': signature, 'observations': 1, 'first_at': now}
    record['last_at'] = now
    record['final'] = bool(final)
    state[candidate_id] = record
    return record


def media_inventory(settings, job_id):
    area = settings.root / 'media' / job_id
    if not area.exists():
        return [], None
    need(not area.is_symlink(), 'MEDIA_AREA_INVALID')
    files = sorted(str(part.relative_to(area)) for part in area.rglob('*') if part.is_file())
    for name in files:
        need(not name.endswith(('.part', '.mp4', '.m4a')) and name.split('/')[-1] != 'verified.json',
             'MEDIA_DOWNLOAD_PRESENT')
    official = sorted(area.rglob('official-source-*.json'))
    evidence = None
    if len(official) == 1 and official[0].is_file():
        body = official[0].read_bytes()
        evidence = {'file': official[0].name, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
    return files, evidence


def rebases_today(store, candidate_id, now):
    rows = store.query('SELECT at FROM audit WHERE action=? AND target=?', (VERSION, candidate_id))
    return sum(1 for row in rows if now - row['at'] <= 86400)


def inspect_crash(settings, store, candidate, now):
    """Requeue one crashed, unverified download that still has attempts left."""
    job_id = candidate['download_job_id']
    need(job_id, 'NO_LINKED_DOWNLOAD')
    download = store.get(job_id)
    need(download is not None and
         (download['job_type'], download['source_platform'], download['source_id']) ==
         (DOWNLOAD_JOB, 'youtube', candidate['source_id']), 'DOWNLOAD_JOB_CHANGED')
    need((download['state'], download['error_code']) == ('failed', CRASH), 'DOWNLOAD_NOT_CRASHED')
    need(download['attempts'] < settings.config['max_attempts'], 'ATTEMPTS_EXHAUSTED')
    need(download['lease_owner'] is None and download['lease_until'] is None, 'DOWNLOAD_LEASED')
    need(candidate['publication_job_id'] is None, 'PUBLICATION_PRESENT')
    need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",
                         (candidate['source_id'],)), 'PUBLICATION_PRESENT')
    plan = store.get(candidate['plan_job_id'])
    need(plan is not None and plan['state'] == 'completed' and
         plan['result'].get('replay_fifo_plan_v4', {}).get('candidate_id') == candidate['candidate_id'],
         'PLAN_BINDING_CHANGED')
    area = settings.root / 'media' / job_id / 'youtube-whole-v4'
    partials = sorted(part.stat().st_size for part in area.glob('*.part')) if area.is_dir() else []
    need(partials and min(partials) > 0, 'PARTIAL_MISSING')
    need(not (area / 'verified.json').exists() and not (area / 'original.mp4').exists(), 'MEDIA_ALREADY_VERIFIED')
    resumed = store.query('SELECT at FROM audit WHERE action=? AND target=?', (VERSION, job_id))
    need(sum(1 for row in resumed if now - row['at'] <= 86400) < MAX_RESUMES_PER_DAY, 'RESUME_LIMIT_REACHED')
    evidence = {'version': VERSION, 'kind': 'crash_resume', 'candidate_id': candidate['candidate_id'],
        'source_id': candidate['source_id'], 'job_id': job_id, 'attempts': download['attempts'],
        'next_attempt': download['attempts'] + 1, 'partial_bytes': partials,
        'error': CRASH, 'publications_before': 0, 'media_deleted': False, 'download_created': False}
    return {'action': 'auto', 'reason': None, 'tier': 'crash_resume', 'evidence': evidence,
            'value': {'kind': 'resume_download', 'job': download, 'candidate': candidate,
                      'partial_bytes': partials, 'evidence': evidence}}


def inspect(settings, store, candidate_id, policy, now, state):
    """Classify one held candidate into a rebase, an approval package or a hold."""
    rows = store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?', (candidate_id,))
    need(len(rows) == 1, 'CANDIDATE_MISSING')
    candidate = rows[0]
    if candidate['state'] == 'hold' and candidate['reason'] == CRASH:
        return inspect_crash(settings, store, candidate, now)
    need(candidate['state'] == 'hold' and candidate['reason'] == ERROR, 'CANDIDATE_NOT_HELD')
    need(candidate['publication_job_id'] is None and candidate['refresh_job_id'] is None, 'CANDIDATE_ALREADY_LINKED')
    need(candidate['policy_sha256'] == policy['_sha256'], 'POLICY_SHA_CHANGED')
    job = store.get(candidate['plan_job_id']) if candidate['plan_job_id'] else None
    if job:
        need((job['source_platform'], job['source_id'], job['job_type'], job['queue'],
              job['policy_version']) == ('youtube', candidate['source_id'], PLAN_JOB, 'video', 'replay-fifo-v4'),
             'JOB_NOT_PLAN_V4')
        need(job['payload']['fifo_v4']['candidate_id'] == candidate_id, 'JOB_PAYLOAD_MISMATCH')
    waits = [] if job is None else store.query('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?',
                                               (job['job_id'],))
    wait = waits[0] if waits else None
    download = store.get(candidate['download_job_id']) if candidate['download_job_id'] else None
    download_failed = download is not None and download['state'] in ('awaiting_review', 'failed', 'auth_expired') \
        and download['error_code'] == ERROR and download['lease_owner'] is None
    child = None
    if download_failed:
        # The hold is owned by the stopped download; the plan job already completed.
        need(job is not None and job['state'] == 'completed', 'PLAN_JOB_NOT_COMPLETED')
    elif wait:
        need(wait['state'] == 'hold' and wait['active_child_id'], 'WAIT_STATE_CHANGED')
        need((job['state'], job['stage']) == ('upload_pending', 'readiness_hold_v4'), 'WAIT_JOB_STATE_CHANGED')
        child = store.get(wait['active_child_id'])
        need(child and child['state'] in ('failed', 'auth_expired', 'awaiting_review', 'publish_unknown') and
             child['error_code'] == ERROR and child['attempts'] >= 1 and
             child['lease_owner'] is None and child['lease_until'] is None, 'WAIT_CHILD_CHANGED')
    elif job:
        need(job['error_code'] == ERROR and job['lease_owner'] is None and job['lease_until'] is None and
             job['external_publish_id'] is None, 'JOB_STATE_CHANGED')
        need(job['state'] in ('awaiting_review', 'failed', 'upload_pending', 'retry_scheduled') and
             job['stage'] != 'readiness_hold_v4', 'JOB_STATE_CHANGED')
    rows = store.query('SELECT * FROM youtube_videos WHERE video_id=?', (candidate['source_id'],))
    need(len(rows) == 1, 'OFFICIAL_SOURCE_MISSING')
    video = rows[0]
    need((video['state'], video['source_api'], video['channel_id']) == ('ended', 'youtube-data-v3-official',
                                                                        candidate['channel_id']), 'OFFICIAL_SOURCE_CHANGED')
    need(0 <= now - video['last_api_check'] <= policy['source_max_age_seconds'], 'OFFICIAL_SOURCE_NOT_FRESH')
    need(now - video['last_api_check'] <= 240, 'OFFICIAL_SOURCE_TOO_CLOSE_TO_STALENESS')
    identity, payload = ReplayFIFOV4(settings, store)._source(video, policy)
    old = json.loads(candidate['source_payload'])
    need('raw' in old, 'SNAPSHOT_UNREADABLE')
    old_raw, new_raw = old['raw'], payload['raw']
    old_identity = {'source_id': old_raw['id'], 'channel_id': old_raw['snippet']['channelId'],
        'actual_start': stamp(old_raw['liveStreamingDetails']['actualStartTime']),
        'actual_end': stamp(old_raw['liveStreamingDetails']['actualEndTime']),
        'duration': observed_duration(old_raw['contentDetails']['duration']),
        'privacy_status': old_raw['status']['privacyStatus']}
    delta = identity['duration'] - old_identity['duration']
    evidence = {'version': VERSION, 'source_id': candidate['source_id'], 'channel_id': candidate['channel_id'],
        'candidate_id': candidate_id, 'plan_job_id': candidate['plan_job_id'],
        'download_job_id': candidate['download_job_id'],
        'previous': {'identity_sha256': candidate['identity_sha256'],
            'source_metadata_sha256': candidate['source_metadata_sha256'],
            'duration': old_raw['contentDetails']['duration'],
            'definition': old_raw['contentDetails'].get('definition'),
            'upload_status': old_raw['status'].get('uploadStatus'),
            'published_at': old_raw['snippet']['publishedAt'], 'title': old_raw['snippet']['title'],
            'actual_start': old_identity['actual_start'], 'actual_end': old_identity['actual_end']},
        'current': {'identity_sha256': sha(identity),
            'source_metadata_sha256': hashlib.sha256(encode(new_raw).encode()).hexdigest(),
            'duration': new_raw['contentDetails']['duration'],
            'definition': new_raw['contentDetails'].get('definition'),
            'upload_status': new_raw['status'].get('uploadStatus'),
            'published_at': new_raw['snippet']['publishedAt'], 'title': new_raw['snippet']['title'],
            'actual_start': identity['actual_start'], 'actual_end': identity['actual_end'],
            'observed_at': video['last_api_check']},
        'field_diff': leaf_diff(old_raw, new_raw), 'duration_delta_seconds': delta,
        'admitted_duration_seconds': MAX_DURATION_REVISION_SECONDS,
        'same_video_same_channel_same_live_window': True,
        'downloads_before': 0 if download is None else 1, 'publications_before': 0}
    observation = observe(state, candidate_id, observation_signature(new_raw, identity), now,
                          final=new_raw['status'].get('uploadStatus') == 'processed')
    evidence['observations'] = {'count': observation['observations'], 'age_hours': round(
        (now - old_identity['actual_end']) / 3600, 1)}
    changed = [key for key in ('source_id', 'channel_id', 'actual_start', 'actual_end')
               if identity[key] != old_identity[key]]
    hold = lambda reason, tier='hold': {'action': 'hold', 'reason': reason, 'tier': tier, 'evidence': evidence}
    if changed or identity['privacy_status'] != 'public':
        return hold('STABLE_IDENTITY_CHANGED')
    disallowed = sorted(path for path in evidence['field_diff'] if not mutable(path))
    if disallowed:
        return hold('NON_MUTABLE_FIELD_CHANGED:' + ','.join(disallowed))
    if abs(delta) <= MAX_DURATION_REVISION_SECONDS:
        raise Skip('DURATION_WITHIN_ADMITTED_WINDOW')
    if sha(old_identity) != candidate['identity_sha256']:
        return hold('REGISTERED_IDENTITY_TAMPERED')
    if hashlib.sha256(encode(old_raw).encode()).hexdigest() != candidate['source_metadata_sha256']:
        return hold('REGISTERED_METADATA_TAMPERED')
    try:
        ReplayFIFOV4(settings, store).observed_identity(candidate, video, policy)
    except HubError as exc:
        need(exc.code == ERROR, 'UNEXPECTED_OBSERVATION_ERROR')
    else:
        raise Skip('CANDIDATE_NOT_BLOCKED')
    if job:
        need(job['payload']['raw'] == old_raw and
             job['payload']['fifo_v4']['identity_sha256'] == candidate['identity_sha256'] and
             job['payload']['fifo_v4']['source_metadata_sha256'] == candidate['source_metadata_sha256'],
             'JOB_FIFO_BINDING_CHANGED')
    if store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",
                   (candidate['source_id'],)):
        return hold('PUBLICATION_PRESENT')
    if store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?",
                   (candidate['source_id'],)):
        return hold('TOMBSTONE_PRESENT')
    marks = ','.join('?' for _ in ASSET_JOBS)
    if store.query(f"SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type IN ({marks})"
                   " AND job_id NOT IN (?,?)", (candidate['source_id'], *ASSET_JOBS,
                                                candidate['plan_job_id'] or '', candidate['download_job_id'] or '')):
        return hold('EXISTING_ASSET_JOB_PRESENT')
    files, registered_evidence = ([], None)
    if job:
        files, registered_evidence = media_inventory(settings, job['job_id'])
        need(not (settings.root / 'media' / job['job_id'] / 'youtube-whole-v4').exists(), 'MEDIA_DOWNLOAD_PRESENT')
    evidence['media_files'] = files
    evidence['registered_observation_evidence'] = registered_evidence
    if rebases_today(store, candidate_id, now) >= MAX_REBASES_PER_DAY:
        return hold('REBASE_LIMIT_REACHED')
    old_upload, new_upload = old_raw['status'].get('uploadStatus'), new_raw['status'].get('uploadStatus')
    finalised = observation['observations'] >= STABLE_OBSERVATIONS and \
        now - old_identity['actual_end'] >= STABLE_FINAL_HOURS * 3600
    if old_upload == 'uploaded' and new_upload == 'processed':
        tier, auto_max = 'post_processing', AUTO_PROCESSING_MAX_SECONDS
    elif old_upload == 'processed' and new_upload == 'processed':
        tier, auto_max = 'finalized_revision', AUTO_FINALIZED_MAX_SECONDS
    elif old_upload == new_upload == 'uploaded' and finalised and delta < 0:
        tier, auto_max = 'stable_finalised', AUTO_FINALIZED_MAX_SECONDS
    else:
        return hold('UNSUPPORTED_TRANSITION')
    evidence['tier'] = tier
    if abs(delta) > APPROVAL_MAX_SECONDS:
        return hold('LARGE_REVISION', tier)
    if download is not None:
        settled = download['state'] in ('awaiting_review', 'failed', 'auth_expired') and \
            download['error_code'] == ERROR and download['lease_owner'] is None
        verified = (settings.root / 'media' / download['job_id'] / 'youtube-whole-v4' / 'verified.json').exists()
        if not settled or verified:
            raise Skip('DOWNLOAD_ACTIVE_OR_VERIFIED')
        if delta > 0 or abs(delta) > auto_max:
            evidence['reason'] = 'DOWNLOAD_REPLAN_REQUIRED'
            return {'action': 'approval', 'reason': 'DOWNLOAD_REPLAN_REQUIRED', 'tier': tier, 'evidence': evidence}
        return {'action': 'auto', 'reason': None, 'tier': tier, 'evidence': evidence,
                'value': build_replan_value(candidate, job, download, payload, identity, evidence, now)}
    value = build_value(candidate_id, candidate, job, wait, child, payload, identity, evidence, now)
    if delta > 0 or abs(delta) > auto_max:
        return {'action': 'approval', 'reason': 'GROWTH_REQUIRES_APPROVAL' if delta > 0 else 'APPROVAL_REQUIRED',
                'tier': tier, 'evidence': evidence, 'value': value}
    return {'action': 'auto', 'reason': None, 'tier': tier, 'evidence': evidence, 'value': value}


def build_value(candidate_id, candidate, job, wait, child, payload, identity, evidence, now):
    new_metadata_sha = evidence['current']['source_metadata_sha256']
    new_identity_sha = evidence['current']['identity_sha256']
    record = result = None
    if wait:
        record = deepcopy(json.loads(wait['record']))
        record['previous_hold'] = {'state': record['state'], 'error_code': record['error_code'],
            'finished_at': record.get('finished_at'), 'child_job_id': wait['active_child_id'],
            'child_error_code': child['error_code']}
        record.pop('error_code', None)
        record.pop('finished_at', None)
        record.update(state='waiting_readiness', next_check=now)
        record['source_snapshot_rebase'] = {'version': VERSION, 'rebased_at': now,
            'tier': evidence['tier'], 'previous_identity_sha256': candidate['identity_sha256'],
            'identity_sha256': new_identity_sha,
            'previous_metadata_sha256': candidate['source_metadata_sha256'], 'metadata_sha256': new_metadata_sha,
            'duration_delta_seconds': evidence['duration_delta_seconds'],
            'admitted_duration_seconds': MAX_DURATION_REVISION_SECONDS,
            'attempts_preserved': True, 'human_review': False, 'publication_authorized': False}
        result = {**job['result'], WAIT: record}
    job_payload = None
    if job:
        job_payload = {**deepcopy(payload), 'fifo_v4': {**job['payload']['fifo_v4'],
            'identity_sha256': new_identity_sha, 'source_metadata_sha256': new_metadata_sha}}
    kind = 'wait' if wait else 'job' if job else 'none'
    return {'kind': kind, 'candidate': candidate, 'job': job, 'wait': wait, 'payload': payload,
        'identity': identity, 'job_payload': job_payload, 'record': record, 'result': result,
        'evidence': evidence, 'payload_sha256': digest(job_payload) if job_payload else None}


def build_replan_value(candidate, job, download, payload, identity, evidence, now):
    """Retire one failed, media-less download and re-run the same plan job."""
    need(job is not None and job['job_type'] == PLAN_JOB and job['state'] == 'completed',
         'PLAN_JOB_NOT_COMPLETED')
    need(candidate['download_job_id'] == download['job_id'], 'DOWNLOAD_LINK_CHANGED')
    previous = job['result'].get('replay_fifo_plan_v4') or {}
    evidence['previous_plan'] = {'state': previous.get('state'), 'plan_sha256': previous.get('plan_sha256')}
    evidence['previous_download'] = {'job_id': download['job_id'], 'state': download['state'],
        'error_code': download['error_code'], 'attempts': download['attempts']}
    evidence['reason'] = 'DOWNLOAD_REPLAN'
    job_payload = {**deepcopy(payload), 'fifo_v4': {**job['payload']['fifo_v4'],
        'identity_sha256': evidence['current']['identity_sha256'],
        'source_metadata_sha256': evidence['current']['source_metadata_sha256']}}
    return {'kind': 'replan', 'candidate': candidate, 'job': job, 'download': download,
        'payload': payload, 'identity': identity, 'job_payload': job_payload,
        'evidence': evidence, 'payload_sha256': digest(job_payload)}


def _rebase_for_replan(db, value, now):
    candidate, download = value['candidate'], value['download']
    current = value['evidence']['current']
    changed = db.execute("""UPDATE replay_fifo_v4 SET source_payload=?,source_metadata_sha256=?,identity_sha256=?,
        state='plan_queued',reason=NULL,download_job_id=NULL,updated_at=? WHERE candidate_id=? AND state='hold'
        AND reason=? AND updated_at=? AND identity_sha256=? AND plan_job_id=? AND download_job_id=?""",
        (encode(value['payload']), current['source_metadata_sha256'], current['identity_sha256'], now,
         candidate['candidate_id'], ERROR, candidate['updated_at'], candidate['identity_sha256'],
         candidate['plan_job_id'], download['job_id'])).rowcount
    need(changed == 1, 'CANDIDATE_UPDATE_CONFLICT')


def apply_replan(settings, store, value, *, now, maintenance_reference):
    fifo = ReplayFIFOV4(settings, store)
    outcome = deepcopy(value['evidence'])
    job, download = value['job'], value['download']
    with store.transaction() as db:
        _rebase_for_replan(db, value, now)
        retired = fifo.retire_asset(db, download['job_id'], expected_state=download['state'],
            expected_error=ERROR, reason='DOWNLOAD_REPLAN', reference=maintenance_reference, now=now)
        changed = db.execute("""UPDATE jobs SET payload=?,result='{}',state='pending',stage='discovered',
            error_code=NULL,next_run=?,updated_at=? WHERE job_id=? AND state='completed' AND attempts=?
            AND updated_at=? AND payload=? AND result=?""",
            (encode(value['job_payload']), now, now, job['job_id'], job['attempts'], job['updated_at'],
             encode(job['payload']), encode(job['result']))).rowcount
        need(changed == 1, 'PLAN_JOB_UPDATE_CONFLICT')
        outcome.update(maintenance_reference=maintenance_reference, applied_at=now, attempts_reset=False,
            source_rows_deleted=0, publication_created=False, download_created=False,
            retired_job_id=download['job_id'], retired=retired)
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'system:replay-source-reconcile', VERSION, value['candidate']['candidate_id'], encode(outcome)))
    candidate = fifo.candidate(value['candidate']['candidate_id'])
    need(candidate['state'] == 'plan_queued' and candidate['download_job_id'] is None,
         'POST_APPLY_CANDIDATE_CHANGED')
    plan = store.get(job['job_id'])
    need(plan['state'] == 'pending' and plan['payload'] == value['job_payload'] and plan['result'] == {},
         'POST_APPLY_PLAN_CHANGED')
    with store.transaction() as db:
        need(fifo._existing(db, candidate['source_id'], except_jobs=(job['job_id'],)) is None,
             'POST_APPLY_SOURCE_STILL_BLOCKED')
    return {'candidate_id': candidate['candidate_id'], 'source_id': candidate['source_id'],
        'kind': 'replan', 'retired_download_job_id': download['job_id'],
        'plan_job_id': job['job_id'], 'candidate_state': candidate['state'],
        'plan_job_state': plan['state'], 'attempts_preserved': job['attempts'],
        'download_created': False, 'publication_created': False,
        'receipt': receipt(settings, outcome, now)}


def _rebase_candidate(db, value, now):
    candidate = value['candidate']
    current = value['evidence']['current']
    state = {'wait': 'waiting_readiness', 'job': 'plan_queued', 'none': 'registered'}[value['kind']]
    changed = db.execute("""UPDATE replay_fifo_v4 SET source_payload=?,source_metadata_sha256=?,identity_sha256=?,
        state=?,reason=NULL,updated_at=? WHERE candidate_id=? AND state='hold' AND reason=? AND updated_at=?
        AND identity_sha256=? AND plan_job_id IS ? AND publication_job_id IS NULL""",
        (encode(value['payload']), current['source_metadata_sha256'], current['identity_sha256'], state,
         now, candidate['candidate_id'], ERROR, candidate['updated_at'], candidate['identity_sha256'],
         candidate['plan_job_id'])).rowcount
    need(changed == 1, 'CANDIDATE_UPDATE_CONFLICT')


def _rebind_job(db, value, now):
    job = value['job']
    if value['kind'] == 'wait':
        state, stage, code = 'retry_scheduled', 'waiting_readiness_v4', 'WAITING_YOUTUBE_READINESS'
        next_run = value['wait']['deadline'] + 60
        result = value['result']
    else:
        state, stage, code = 'pending', job['stage'], None
        next_run = now
        result = job['result']
    changed = db.execute("""UPDATE jobs SET payload=?,result=?,state=?,stage=?,error_code=?,next_run=?,updated_at=?
        WHERE job_id=? AND state=? AND error_code=? AND attempts=? AND updated_at=? AND payload=? AND result=?
        AND lease_owner IS NULL AND lease_until IS NULL""",
        (encode(value['job_payload']), encode(result), state, stage, code, next_run, now, job['job_id'],
         job['state'], ERROR, job['attempts'], job['updated_at'], encode(job['payload']),
         encode(job['result']))).rowcount
    need(changed == 1, 'JOB_UPDATE_CONFLICT')


def _reopen_wait(db, value, now, binding_sha):
    wait = value['wait']
    changed = db.execute("""UPDATE youtube_readiness_waits_v4 SET state='waiting_readiness',active_child_id=NULL,
        next_check=?,binding_sha256=?,payload_sha256=?,record=? WHERE parent_job_id=? AND wait_id=? AND state='hold'
        AND binding_sha256=? AND payload_sha256=? AND active_child_id=?""",
        (now, binding_sha, value['payload_sha256'], encode(value['record']), value['job']['job_id'],
         wait['wait_id'], wait['binding_sha256'], wait['payload_sha256'], wait['active_child_id'])).rowcount
    need(changed == 1, 'WAIT_UPDATE_CONFLICT')


def binding_from_copy(settings, store, value, now):
    area = tempfile.mkdtemp(prefix='lighthouse-reconcile-')
    copy = os.path.join(area, 'hub.sqlite3')
    try:
        source = sqlite3.connect(store.path)
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        copied = Store(copy)
        with copied.transaction() as db:
            _rebase_candidate(db, value, now)
            _rebind_job(db, value, now)
        job = copied.get(value['job']['job_id'])
        bound = digest(YouTubeWholeReplayPrepare(settings, copied).resource_binding(job))
        WholeReadinessCoordinatorV4(settings, copied)._binding(job, bound)
        with copied.transaction() as db:
            _reopen_wait(db, value, now, bound)
        WholeReadinessCoordinatorV4(settings, copied)._binding(copied.get(value['job']['job_id']), bound)
        return bound
    finally:
        for name in os.listdir(area):
            os.unlink(os.path.join(area, name))
        os.rmdir(area)


def receipt(settings, outcome, now):
    folder = settings.root / 'data' / 'ops-maintenance'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / ('source-snapshot-reconcile-%s-%d.json' % (outcome['candidate_id'], int(now)))
    body = encode(outcome).encode()
    with open(path, 'wb') as stream:
        stream.write(body)
    os.chmod(path, 0o600)
    return {'path': str(path), 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}


def decision_package(settings, store, decision, now, state):
    """Persist the exact evidence and the one command that would settle it."""
    evidence = decision['evidence']
    candidate_id = evidence['candidate_id']
    record = state.get(candidate_id, {})
    if record.get('package_signature') == record.get('signature') and now - record.get('package_at', 0) < 21600:
        return {'deduplicated': True, 'signature': record.get('signature')}
    folder = settings.root / 'data' / 'ops-maintenance' / 'decisions'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = {'version': VERSION, 'action': decision['action'], 'reason': decision['reason'],
        'tier': decision['tier'], 'created_at': now, 'candidate_id': candidate_id,
        'source_id': evidence['source_id'], 'evidence': evidence,
        'approve_command': None if decision['action'] != 'approval' or not decision.get('value') else
            'python3 /opt/lighthouse-hub/backups/ops-replay-reconcile/reconcile_replay_source_revision_v1.py '
            '--approve %s --maintenance-reference "<decision>"' % candidate_id,
        'note': 'approval requires a human decision; hold reasons are not approvable by this tool'}
    path = folder / ('%s-%d.json' % (candidate_id, int(now)))
    raw = encode(body).encode()
    with open(path, 'wb') as stream:
        stream.write(raw)
    os.chmod(path, 0o600)
    store.event('review.required', {'code': decision['reason'], 'tier': decision['tier'],
        'candidate_id': candidate_id, 'source_id': evidence['source_id'],
        'command': body['approve_command'], 'queue': 'video'},
        dedup_key='reconcile:%s:%s' % (candidate_id, decision['reason']), interval=21600)
    record['package_signature'] = record.get('signature')
    record['package_at'] = now
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def apply_resume(store, value, *, now, maintenance_reference):
    job, candidate = value['job'], value['candidate']
    outcome = deepcopy(value['evidence'])
    outcome.update(maintenance_reference=maintenance_reference, applied_at=now,
                   attempts_reset=False, media_deleted=False, publication_created=False)
    with store.transaction() as db:
        changed = db.execute("""UPDATE jobs SET state='retry_scheduled',error_code=NULL,next_run=?,updated_at=?
            WHERE job_id=? AND state='failed' AND error_code=? AND attempts=? AND updated_at=?
            AND lease_owner IS NULL AND lease_until IS NULL""",
            (now, now, job['job_id'], CRASH, job['attempts'], job['updated_at'])).rowcount
        need(changed == 1, 'JOB_UPDATE_CONFLICT')
        changed = db.execute("""UPDATE replay_fifo_v4 SET state='download_queued',reason=NULL,updated_at=?
            WHERE candidate_id=? AND state='hold' AND reason=? AND updated_at=? AND download_job_id=?""",
            (now, candidate['candidate_id'], CRASH, candidate['updated_at'], job['job_id'])).rowcount
        need(changed == 1, 'CANDIDATE_UPDATE_CONFLICT')
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'system:replay-source-reconcile', VERSION, job['job_id'], encode(outcome)))
    need(store.get(job['job_id'])['state'] == 'retry_scheduled', 'POST_APPLY_JOB_CHANGED')
    return {'candidate_id': candidate['candidate_id'], 'source_id': candidate['source_id'],
        'kind': 'resume_download', 'job_id': job['job_id'], 'attempts_preserved': job['attempts'],
        'next_attempt': job['attempts'] + 1, 'candidate_state': 'download_queued',
        'job_state': 'retry_scheduled', 'partial_bytes': value['partial_bytes'],
        'media_deleted': False, 'download_created': False, 'publication_created': False}


def apply(settings, store, value, *, now, maintenance_reference, binding_sha):
    if value['kind'] == 'resume_download':
        return apply_resume(store, value, now=now, maintenance_reference=maintenance_reference)
    if value['kind'] == 'replan':
        return apply_replan(settings, store, value, now=now, maintenance_reference=maintenance_reference)
    outcome = deepcopy(value['evidence'])
    with store.transaction() as db:
        _rebase_candidate(db, value, now)
        if value['job']:
            _rebind_job(db, value, now)
        if value['wait']:
            _reopen_wait(db, value, now, binding_sha)
        outcome.update(binding_sha256=binding_sha, maintenance_reference=maintenance_reference,
            attempts_reset=False, source_rows_deleted=0, publication_created=False, download_created=False,
            applied_at=now)
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'system:replay-source-reconcile', VERSION, value['candidate']['candidate_id'], encode(outcome)))
    fifo = ReplayFIFOV4(settings, store)
    candidate = fifo.candidate(value['candidate']['candidate_id'])
    fifo._fresh(candidate, fifo.policy())
    need(candidate['state'] in ('waiting_readiness', 'plan_queued', 'registered') and candidate['reason'] is None,
         'POST_APPLY_CANDIDATE_CHANGED')
    if value['wait']:
        job = store.get(value['job']['job_id'])
        need(digest(YouTubeWholeReplayPrepare(settings, store).resource_binding(job)) == binding_sha,
             'POST_APPLY_BINDING_CHANGED')
        WholeReadinessCoordinatorV4(settings, store)._binding(job, binding_sha)
        wait = store.query('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?', (job['job_id'],))[0]
        need(wait['state'] == 'waiting_readiness' and wait['binding_sha256'] == binding_sha and
             wait['active_child_id'] is None, 'POST_APPLY_WAIT_CHANGED')
    return {'candidate_id': candidate['candidate_id'], 'source_id': candidate['source_id'],
        'kind': value['kind'], 'duration_before': value['evidence']['previous']['duration'],
        'duration_after': value['evidence']['current']['duration'],
        'duration_delta_seconds': value['evidence']['duration_delta_seconds'],
        'identity_sha256': candidate['identity_sha256'], 'candidate_state': candidate['state'],
        'job_state': None if not value['job'] else store.get(value['job']['job_id'])['state'],
        'attempts_preserved': None if not value['job'] else value['job']['attempts'],
        'binding_sha256': binding_sha, 'download_created': False, 'publication_created': False,
        'receipt': receipt(settings, outcome, now)}


def decide(settings, store, candidate_id, *, now, state):
    policy = ReplayFIFOV4(settings, store).policy()
    decision = inspect(settings, store, candidate_id, policy, now, state)
    if decision['action'] in ('auto', 'approval') and decision.get('value') and decision['value'].get('job') \
            and decision['value']['kind'] not in ('resume_download', 'replan'):
        decision['binding_sha256'] = binding_from_copy(settings, store, decision['value'], now)
    return decision


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--maintenance-reference', required=True)
    parser.add_argument('--candidate')
    parser.add_argument('--approve')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max', type=int, default=MAX_CANDIDATES)
    args = parser.parse_args()
    if not (runtime_allowed() and __debug__):
        raise SystemExit('APP_IDENTITY_REQUIRED')
    if not isinstance(args.maintenance_reference, str) or not 0 < len(args.maintenance_reference.strip()) <= 2048:
        raise SystemExit('MAINTENANCE_REFERENCE_REQUIRED')
    if type(args.max) is not int or not 1 <= args.max <= 200:
        raise SystemExit('MAX_INVALID')
    settings = Settings.load()
    store = Store(settings.db_path)
    now = time.time()
    state = load_state(settings)
    summary = {'version': VERSION, 'applied': bool(args.apply or args.approve), 'scanned': 0,
               'rebased': [], 'approvals': [], 'held': [], 'skipped': [], 'errors': [], 'at': now}
    try:
        with resource_guard(settings.root / 'data/locks/replay-fifo-dispatch-v4'):
            if args.approve:
                rows = [{'candidate_id': args.approve}]
            elif args.candidate:
                rows = [{'candidate_id': args.candidate}]
            else:
                rows = store.query("SELECT candidate_id FROM replay_fifo_v4 WHERE state='hold' AND reason IN (?,?)"
                                   ' ORDER BY updated_at LIMIT ?', (ERROR, CRASH, args.max))
            summary['scanned'] = len(rows)
            for row in rows:
                candidate_id = row['candidate_id']
                try:
                    decision = decide(settings, store, candidate_id, now=now, state=state)
                    if decision['action'] == 'auto':
                        if args.apply or args.approve:
                            summary['rebased'].append(apply(settings, store, decision['value'], now=now,
                                maintenance_reference=args.maintenance_reference,
                                binding_sha=decision.get('binding_sha256')))
                        else:
                            summary['approvals'].append({'candidate_id': candidate_id, 'tier': decision['tier'],
                                'duration_delta_seconds': decision['evidence']['duration_delta_seconds'],
                                'would': 'auto_when_applied'})
                        continue
                    if decision['action'] == 'approval':
                        entry = {'candidate_id': candidate_id, 'reason': decision['reason'],
                                 'tier': decision['tier'],
                                 'duration_delta_seconds': decision['evidence']['duration_delta_seconds']}
                        if args.approve:
                            if not decision.get('value'):
                                raise Skip('DOWNSTREAM_REPLAN_NOT_SUPPORTED')
                            summary['rebased'].append(apply(settings, store, decision['value'], now=now,
                                maintenance_reference=args.maintenance_reference,
                                binding_sha=decision.get('binding_sha256')))
                            continue
                        entry['package'] = decision_package(settings, store, decision, now, state)
                        summary['approvals'].append(entry)
                        continue
                    entry = {'candidate_id': candidate_id, 'reason': decision['reason'],
                             'tier': decision['tier']}
                    entry['package'] = decision_package(settings, store, decision, now, state)
                    summary['held'].append(entry)
                except Skip as exc:
                    summary['skipped'].append({'candidate_id': candidate_id, 'reason': exc.code})
                except HubError as exc:
                    summary['errors'].append({'candidate_id': candidate_id, 'code': exc.code})
                except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
                    summary['errors'].append({'candidate_id': candidate_id,
                        'code': 'UNREADABLE_TARGET:' + type(exc).__name__})
    except HubError as exc:
        if exc.code != 'HEAVY_RESOURCE_BUSY':
            raise
        summary['busy'] = True
    save_state(settings, state, now)
    print(encode(summary))
    if summary['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
