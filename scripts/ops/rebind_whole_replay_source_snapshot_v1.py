"""Rebase one whole-replay candidate onto a post-processing official revision.

The FIFO identity gate admits a duration revision of at most five seconds from the
registered snapshot. YouTube can finish processing a finished live archive after
registration: the same video id, channel, actual start and actual end then carry a
slightly shorter HD rendition, together with a new etag, publishedAt, uploadStatus
and thumbnail URLs. That is a metadata revision of the same replay, but the gate
holds the candidate with REPLAY_FIFO_SOURCE_IDENTITY_CHANGED.

This operator repairs exactly one such candidate. It proves the stable identity is
unchanged, keeps the complete before/after observation in audit and in a receipt
file, replaces the stale registered snapshot with the current official one, and
rebinds the *same* plan job (same job id, created_at and attempts) and the *same*
readiness wait so the ordinary coordinator resumes. It never deletes rows, never
silently clears the error, never resets attempts, never creates a download or a
publication, and never changes the identity gate or its five second window.
"""
import argparse
from copy import deepcopy
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
from services.hub.replay_fifo_v4 import (ASSET_JOBS, MAX_DURATION_REVISION_SECONDS,
    PLAN_JOB, ReplayFIFOV4, observed_duration, sha, stamp)
from services.hub.replay_multipart_v4 import digest
from services.hub.youtube_download_v4 import YouTubeWholeReplayPrepare
from services.hub.youtube_readiness_v4 import WAIT, WholeReadinessCoordinatorV4

VERSION = 'rebind-whole-replay-source-snapshot-v1'
ROOT = '/opt/lighthouse-hub'
SOURCE = '2yuDK7sl3lQ'
CHANNEL = 'UCaaaaaaaaaaaaaaaaaaaaaa'
CANDIDATE = '05bd6fef-e1f9-4560-83f7-68c1c343f10d'
JOB = 'ee504a5a-7342-4252-ba2c-f6355e91dae1'
WAIT_ID = 'db8c1e32-ced4-4a57-8eb2-6243ef34d194'
CHILD = 'e1ee86eb-30c3-4d02-949a-ab711fc762de'
ERROR = 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED'
MAX_REBASE_DURATION_SECONDS = 60
# Fields YouTube revises on the same video when it publishes the processed archive.
MUTABLE_PATHS = ('/contentDetails/definition', '/contentDetails/duration', '/etag',
                 '/liveStreamingDetails/activeLiveChatId', '/snippet/publishedAt',
                 '/snippet/title', '/snippet/description', '/status/uploadStatus')
MUTABLE_PREFIXES = ('/snippet/thumbnails/',)


def need(value, code):
    if not value:
        raise RuntimeError(code)


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


def media_inventory(settings):
    area = settings.root / 'media' / JOB
    need(area.is_dir() and not area.is_symlink(), 'MEDIA_AREA_MISSING')
    files = sorted(str(part.relative_to(area)) for part in area.rglob('*') if part.is_file())
    for name in files:
        need(name.startswith('youtube-whole-v4-plan/'), 'UNEXPECTED_MEDIA')
        need(not name.endswith(('.part', '.mp4', '.m4a')) and name.split('/')[-1] != 'verified.json',
             'MEDIA_DOWNLOAD_PRESENT')
    need(not (settings.root / 'media' / CHILD).exists() or
         not any(part.is_file() for part in (settings.root / 'media' / CHILD).rglob('*')),
         'READINESS_CHILD_MEDIA_PRESENT')
    official = sorted((area / 'youtube-whole-v4-plan').glob('official-source-*.json'))
    need(len(official) == 1 and official[0].is_file(), 'REGISTERED_OBSERVATION_EVIDENCE_MISSING')
    raw = official[0].read_bytes()
    return files, {'file': official[0].name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def row_dict(row):
    value = dict(row)
    for key in ('payload', 'result'):
        value[key] = json.loads(value[key])
    return value


def snapshot(settings, store):
    """Read-only proof that exactly one stale snapshot can be rebased."""
    need(settings.root.as_posix() == ROOT, 'ROOT_CHANGED')
    fifo = ReplayFIFOV4(settings, store)
    policy = fifo.policy()
    now = time.time()
    jobs = store.query('SELECT * FROM jobs WHERE job_id=?', (JOB,))
    candidates = store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?', (CANDIDATE,))
    waits = store.query('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?', (JOB,))
    children = store.query('SELECT * FROM jobs WHERE job_id=?', (CHILD,))
    videos = store.query('SELECT * FROM youtube_videos WHERE video_id=?', (SOURCE,))
    need(len(jobs) == len(candidates) == len(waits) == len(children) == len(videos) == 1, 'TARGET_ROW_MISSING')
    job, candidate, wait, child, video = row_dict(jobs[0]), candidates[0], waits[0], children[0], videos[0]
    need((job['source_platform'], job['source_id'], job['job_type'], job['queue'], job['policy_version']) ==
         ('youtube', SOURCE, PLAN_JOB, 'video', 'replay-fifo-v4'), 'JOB_NOT_PLAN_V4')
    need((job['state'], job['stage'], job['error_code'], job['attempts']) ==
         ('upload_pending', 'readiness_hold_v4', ERROR, 1), 'JOB_STATE_CHANGED')
    need(job['lease_owner'] is None and job['lease_until'] is None and job['external_publish_id'] is None,
         'JOB_LEASE_OR_PUBLICATION_PRESENT')
    need((candidate['source_id'], candidate['channel_id'], candidate['target_uid'], candidate['state'],
          candidate['reason'], candidate['plan_job_id']) ==
         (SOURCE, CHANNEL, policy['target_site_uid'], 'hold', ERROR, JOB), 'CANDIDATE_STATE_CHANGED')
    need(candidate['download_job_id'] is None and candidate['publication_job_id'] is None and
         candidate['refresh_job_id'] is None, 'CANDIDATE_ALREADY_LINKED')
    need(candidate['policy_sha256'] == policy['_sha256'], 'POLICY_SHA_CHANGED')
    need((wait['wait_id'], wait['state'], wait['active_child_id']) == (WAIT_ID, 'hold', CHILD), 'WAIT_STATE_CHANGED')
    need(wait['binding_sha256'] == digest(YouTubeWholeReplayPrepare(settings, store).resource_binding(job)),
         'WAIT_BINDING_CHANGED')
    need((child['job_type'], child['state'], child['error_code'], child['attempts']) ==
         ('youtube_replay_readiness_v4', 'awaiting_review', ERROR, 1), 'CHILD_STATE_CHANGED')
    need(child['lease_owner'] is None and child['lease_until'] is None, 'CHILD_LEASE_PRESENT')
    need((video['state'], video['source_api'], video['channel_id']) == ('ended', 'youtube-data-v3-official', CHANNEL),
         'OFFICIAL_SOURCE_CHANGED')
    need(0 <= now - video['last_api_check'] <= policy['source_max_age_seconds'], 'OFFICIAL_SOURCE_NOT_FRESH')
    need(now - video['last_api_check'] <= 240, 'OFFICIAL_SOURCE_TOO_CLOSE_TO_STALENESS')
    identity, payload = fifo._source(video, policy)             # production observation gate
    old = json.loads(candidate['source_payload'])
    old_raw, new_raw = old['raw'], payload['raw']
    old_identity = {'source_id': old_raw['id'], 'channel_id': old_raw['snippet']['channelId'],
        'actual_start': stamp(old_raw['liveStreamingDetails']['actualStartTime']),
        'actual_end': stamp(old_raw['liveStreamingDetails']['actualEndTime']),
        'duration': observed_duration(old_raw['contentDetails']['duration']),
        'privacy_status': old_raw['status']['privacyStatus']}
    for key in ('source_id', 'channel_id', 'actual_start', 'actual_end'):
        need(identity[key] == old_identity[key], 'STABLE_IDENTITY_CHANGED')
    need(identity['privacy_status'] == 'public', 'SOURCE_NOT_PUBLIC')
    diff = leaf_diff(old_raw, new_raw)
    disallowed = sorted(path for path in diff if not mutable(path))
    need(not disallowed, 'NON_MUTABLE_FIELD_CHANGED:' + ','.join(disallowed))
    delta = identity['duration'] - old_identity['duration']
    need(abs(delta) > MAX_DURATION_REVISION_SECONDS, 'DURATION_WITHIN_ADMITTED_WINDOW')
    need(abs(delta) <= MAX_REBASE_DURATION_SECONDS, 'DURATION_DRIFT_OUT_OF_SCOPE')
    need(sha(old_identity) == candidate['identity_sha256'], 'REGISTERED_IDENTITY_TAMPERED')
    need(hashlib.sha256(encode(old_raw).encode()).hexdigest() == candidate['source_metadata_sha256'],
         'REGISTERED_METADATA_TAMPERED')
    try:
        fifo.observed_identity(candidate, video, policy)
    except HubError as exc:
        need(exc.code == ERROR, 'UNEXPECTED_OBSERVATION_ERROR')
    else:
        need(False, 'CANDIDATE_NOT_BLOCKED')
    need(job['payload']['raw'] == old_raw and job['payload']['fifo_v4']['candidate_id'] == CANDIDATE and
         job['payload']['fifo_v4']['identity_sha256'] == candidate['identity_sha256'] and
         job['payload']['fifo_v4']['source_metadata_sha256'] == candidate['source_metadata_sha256'] and
         {k: job['payload'].get(k) for k in old if k != 'raw'} == {k: v for k, v in old.items() if k != 'raw'},
         'JOB_FIFO_BINDING_CHANGED')
    need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?", (SOURCE,)),
         'PUBLICATION_PRESENT')
    need(not store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?", (SOURCE,)),
         'TOMBSTONE_PRESENT')
    marks = ','.join('?' for _ in ASSET_JOBS)
    need(not store.query(f"SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type IN ({marks}) AND job_id<>?",
                         (SOURCE, *ASSET_JOBS, JOB)), 'EXISTING_ASSET_JOB_PRESENT')
    need(len(store.query('SELECT 1 FROM replay_fifo_v4 WHERE source_id=?', (SOURCE,))) == 1, 'DUPLICATE_CANDIDATE')
    files, official_evidence = media_inventory(settings)
    record = json.loads(wait['record'])
    need(record['state'] == 'hold' and record['error_code'] == ERROR and record['candidate_id'] == CANDIDATE,
         'WAIT_RECORD_CHANGED')
    new_metadata_sha = hashlib.sha256(encode(new_raw).encode()).hexdigest()
    new_identity_sha = sha(identity)
    job_payload = {**deepcopy(payload), 'fifo_v4': {**job['payload']['fifo_v4'],
        'identity_sha256': new_identity_sha, 'source_metadata_sha256': new_metadata_sha}}
    previous_hold = {'state': record['state'], 'error_code': record['error_code'],
                     'finished_at': record.get('finished_at'), 'child_job_id': CHILD,
                     'child_error_code': child['error_code']}
    record.pop('error_code', None)
    record.pop('finished_at', None)
    record.update(state='waiting_readiness', next_check=now)
    record['previous_hold'] = previous_hold
    record['source_snapshot_rebase'] = {'version': VERSION, 'rebased_at': now,
        'previous_identity_sha256': candidate['identity_sha256'], 'identity_sha256': new_identity_sha,
        'previous_metadata_sha256': candidate['source_metadata_sha256'], 'metadata_sha256': new_metadata_sha,
        'previous_duration_seconds': old_identity['duration'], 'duration_seconds': identity['duration'],
        'duration_delta_seconds': delta, 'admitted_duration_seconds': MAX_DURATION_REVISION_SECONDS,
        'attempts_preserved': True, 'human_review': False, 'publication_authorized': False}
    result = {**job['result'], WAIT: record}
    evidence = {'version': VERSION, 'source_id': SOURCE, 'channel_id': CHANNEL,
        'candidate_id': CANDIDATE, 'job_id': JOB, 'wait_id': WAIT_ID, 'child_job_id': CHILD,
        'previous': {'identity_sha256': candidate['identity_sha256'],
            'source_metadata_sha256': candidate['source_metadata_sha256'],
            'duration': old_raw['contentDetails']['duration'],
            'definition': old_raw['contentDetails'].get('definition'),
            'upload_status': old_raw['status'].get('uploadStatus'),
            'published_at': old_raw['snippet']['publishedAt'], 'title': old_raw['snippet']['title'],
            'actual_start': old_identity['actual_start'], 'actual_end': old_identity['actual_end']},
        'current': {'identity_sha256': new_identity_sha, 'source_metadata_sha256': new_metadata_sha,
            'duration': new_raw['contentDetails']['duration'],
            'definition': new_raw['contentDetails'].get('definition'),
            'upload_status': new_raw['status'].get('uploadStatus'),
            'published_at': new_raw['snippet']['publishedAt'], 'title': new_raw['snippet']['title'],
            'actual_start': identity['actual_start'], 'actual_end': identity['actual_end'],
            'observed_at': video['last_api_check']},
        'field_diff': diff, 'duration_delta_seconds': delta,
        'admitted_duration_seconds': MAX_DURATION_REVISION_SECONDS,
        'same_video_same_channel_same_live_window': True, 'registered_observation_evidence': official_evidence,
        'downloads_before': 0, 'publications_before': 0, 'asset_jobs_before': 0, 'media_files': files,
        'job': {'attempts_before': job['attempts'], 'state_before': job['state'], 'stage_before': job['stage'],
            'state_after': 'retry_scheduled', 'stage_after': 'waiting_readiness_v4'},
        'wait': {'state_before': 'hold', 'state_after': 'waiting_readiness', 'deadline': wait['deadline']}}
    return {'evidence': evidence, 'candidate': candidate, 'job': job, 'wait': wait,
        'payload': payload, 'identity': identity, 'job_payload': job_payload,
        'record': record, 'result': result, 'payload_sha256': digest(job_payload)}


def _rebase_candidate_and_job(db, value, now):
    """Identical row edits for the throwaway binding copy and the real write."""
    candidate, job = value['candidate'], value['job']
    changed = db.execute("""UPDATE replay_fifo_v4 SET source_payload=?,source_metadata_sha256=?,identity_sha256=?,
        state='waiting_readiness',reason=NULL,updated_at=? WHERE candidate_id=? AND state='hold' AND reason=?
        AND updated_at=? AND identity_sha256=? AND plan_job_id=? AND download_job_id IS NULL
        AND publication_job_id IS NULL""",
        (encode(value['payload']), value['evidence']['current']['source_metadata_sha256'],
         value['evidence']['current']['identity_sha256'], now, CANDIDATE, ERROR, candidate['updated_at'],
         candidate['identity_sha256'], JOB)).rowcount
    need(changed == 1, 'CANDIDATE_UPDATE_CONFLICT')
    changed = db.execute("""UPDATE jobs SET payload=?,result=?,state='retry_scheduled',stage='waiting_readiness_v4',
        error_code='WAITING_YOUTUBE_READINESS',next_run=?,updated_at=? WHERE job_id=? AND state='upload_pending'
        AND stage='readiness_hold_v4' AND error_code=? AND attempts=? AND updated_at=? AND payload=? AND result=?
        AND lease_owner IS NULL AND lease_until IS NULL""",
        (encode(value['job_payload']), encode(value['result']), value['wait']['deadline'] + 60, now, JOB, ERROR,
         job['attempts'], job['updated_at'], encode(job['payload']), encode(job['result']))).rowcount
    need(changed == 1, 'JOB_UPDATE_CONFLICT')


def _reopen_wait(db, value, now, binding_sha):
    wait = value['wait']
    changed = db.execute("""UPDATE youtube_readiness_waits_v4 SET state='waiting_readiness',active_child_id=NULL,
        next_check=?,binding_sha256=?,payload_sha256=?,record=? WHERE parent_job_id=? AND wait_id=? AND state='hold'
        AND binding_sha256=? AND payload_sha256=? AND active_child_id=?""",
        (now, binding_sha, value['payload_sha256'], encode(value['record']), JOB, WAIT_ID,
         wait['binding_sha256'], wait['payload_sha256'], CHILD)).rowcount
    need(changed == 1, 'WAIT_UPDATE_CONFLICT')


def binding_from_copy(settings, store, value, now):
    """Use the production binding function on a throwaway copy of the same rows."""
    area = tempfile.mkdtemp(prefix='lighthouse-rebind-')
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
            _rebase_candidate_and_job(db, value, now)
        job = copied.get(JOB)
        bound = digest(YouTubeWholeReplayPrepare(settings, copied).resource_binding(job))
        WholeReadinessCoordinatorV4(settings, copied)._binding(job, bound)
        with copied.transaction() as db:
            _reopen_wait(db, value, now, bound)
        WholeReadinessCoordinatorV4(settings, copied)._binding(copied.get(JOB), bound)
        return bound
    finally:
        for name in os.listdir(area):
            os.unlink(os.path.join(area, name))
        os.rmdir(area)


def apply(store, settings, value, *, now, maintenance_reference, binding_sha):
    need(len(binding_sha) == 64, 'BINDING_SHA_INVALID')
    outcome = deepcopy(value['evidence'])
    with store.transaction() as db:
        _rebase_candidate_and_job(db, value, now)
        _reopen_wait(db, value, now, binding_sha)
        outcome.update(binding_sha256=binding_sha, maintenance_reference=maintenance_reference,
            attempts_reset=False, source_rows_deleted=0, publication_created=False, download_created=False)
        outcome['plan_sha256'] = digest({**value['evidence'], 'binding_sha256': binding_sha})
        outcome['applied_at'] = now
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-authorized-maintenance', VERSION, CANDIDATE, encode(outcome)))
    fifo = ReplayFIFOV4(settings, store)
    policy = fifo.policy()
    candidate = fifo.candidate(CANDIDATE)
    fifo._fresh(candidate, policy)                             # real gate now admits the rebased snapshot
    job = store.get(JOB)
    need(digest(YouTubeWholeReplayPrepare(settings, store).resource_binding(job)) == binding_sha,
         'POST_APPLY_BINDING_CHANGED')
    WholeReadinessCoordinatorV4(settings, store)._binding(job, binding_sha)
    wait = store.query('SELECT * FROM youtube_readiness_waits_v4 WHERE parent_job_id=?', (JOB,))[0]
    need(wait['state'] == 'waiting_readiness' and wait['binding_sha256'] == binding_sha and
         wait['active_child_id'] is None and wait['next_check'] <= time.time(), 'POST_APPLY_WAIT_CHANGED')
    need(candidate['state'] == 'waiting_readiness' and candidate['reason'] is None, 'POST_APPLY_CANDIDATE_CHANGED')
    need(job['state'] == 'retry_scheduled' and job['attempts'] == value['job']['attempts'] and
         job['next_run'] > time.time() + 3600, 'POST_APPLY_JOB_CHANGED')
    return {'state': 'stale_source_snapshot_rebased', 'candidate_id': CANDIDATE, 'job_id': JOB,
        'wait_id': WAIT_ID, 'attempts_preserved': job['attempts'], 'binding_sha256': binding_sha,
        'candidate_state': candidate['state'], 'job_state': job['state'], 'job_stage': job['stage'],
        'next_run': job['next_run'], 'download_created': False, 'publication_created': False,
        'receipt': receipt(settings, outcome, now)}


def receipt(settings, outcome, now):
    folder = settings.root / 'data' / 'ops-maintenance'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / ('source-snapshot-rebase-' + CANDIDATE + '-' + str(int(now)) + '.json')
    body = encode(outcome).encode()
    with open(path, 'wb') as stream:
        stream.write(body)
    os.chmod(path, 0o600)
    return {'path': str(path), 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--maintenance-reference', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-sha256')
    args = parser.parse_args()
    need(runtime_allowed() and __debug__, 'APP_IDENTITY_REQUIRED')
    need(bool(args.apply) == bool(args.plan_sha256), 'EXACT_PLAN_REQUIRED')
    need(isinstance(args.maintenance_reference, str) and 0 < len(args.maintenance_reference.strip()) <= 2048,
         'MAINTENANCE_REFERENCE_REQUIRED')
    settings = Settings.load()
    store = Store(settings.db_path)
    with resource_guard(settings.root / 'data/locks/video'), resource_guard(
            settings.root / 'data/locks/replay-fifo-dispatch-v4'):
        value = snapshot(settings, store)
        now = time.time()
        binding_sha = binding_from_copy(settings, store, value, now)
        value['binding_sha256'] = binding_sha
        plan = digest({**value['evidence'], 'binding_sha256': binding_sha})
        if not args.apply:
            print(encode({'state': 'plan_only', 'version': VERSION, 'plan_sha256': plan,
                'candidate_id': CANDIDATE, 'job_id': JOB, 'wait_id': WAIT_ID,
                'duration_before': value['evidence']['previous']['duration'],
                'duration_after': value['evidence']['current']['duration'],
                'duration_delta_seconds': value['evidence']['duration_delta_seconds'],
                'identity_sha256_before': value['evidence']['previous']['identity_sha256'],
                'identity_sha256_after': value['evidence']['current']['identity_sha256'],
                'binding_sha256': binding_sha, 'attempts': 1, 'download_created': False,
                'publication_created': False, 'field_diff': sorted(value['evidence']['field_diff'])}))
            return
        need(plan == args.plan_sha256, 'REVIEWED_PLAN_CHANGED')
        result = apply(store, settings, value, now=now, maintenance_reference=args.maintenance_reference,
            binding_sha=binding_sha)
        result['plan_sha256'] = plan
        print(encode(result))


if __name__ == '__main__':
    main()
