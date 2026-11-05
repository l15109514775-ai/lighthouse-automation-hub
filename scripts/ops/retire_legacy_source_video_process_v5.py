"""Retire the one stale source-level video_process row superseded by replay V5.

Default is a read-only plan. Apply is CAS-bound to that plan and preserves the
payload, attempts, publications, media, and provider state. It creates no post,
retry, upload, download, or deletion.
"""
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import json
import os
import time
import uuid

from services.hub.db import Store, encode
from services.hub.media_processing import atomic_json

VERSION = 'retire-legacy-source-video-process-v5'
JOB = '32791bc2-8e84-4a52-9246-f524cd8cef81'
SOURCE = 'EoZFofw4OSU'
DOWNLOAD = '5af6b351-e73a-4451-b32a-cc845a2892f1'
GROUP = '1e28dd0f-cc2d-4928-bfd6-e373036c193a'
PUBLICATIONS = ('BV1Ugx26GEwq', 'BV1HuxS66ETH')
OWNER_RESPONSE = '视频上传完并验收后，需要确保旧的视频文件在服务器中被删除，这也是确保工作流可以持久运行的一部分。'
OWNER_REFERENCE = 'chat:2026-11-04:replay-upload-acceptance-and-durable-cleanup'


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def stable_sha(plan):
    value = deepcopy(plan)
    value.pop('planned_at', None)
    return digest(value)


def reference(file):
    file = Path(file)
    need(file.is_absolute() and file.resolve() == file and file.is_file() and
         not any(path.is_symlink() for path in (file, *file.parents)), 'RETIRE_LEGACY_PATH_INVALID')
    raw = file.read_bytes()
    need(0 < len(raw) <= 2_000_000, 'RETIRE_LEGACY_RECORD_SIZE_INVALID')
    return {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def current_state(store, now):
    row = store.get(JOB)
    need(row is not None, 'RETIRE_LEGACY_JOB_MISSING')
    need(
        row['source_platform'] == 'youtube'
        and row['source_id'] == SOURCE
        and row['job_type'] == 'video_process'
        and row['policy_version'] == 'machine-tweet-v3-api'
        and row['queue'] == 'video'
        and row['state'] == 'pending'
        and row['attempts'] == 0
        and row['error_code'] is None
        and row['external_publish_id'] is None
        and row['lease_owner'] is None
        and row['lease_until'] is None
        and row['next_run'] == 0
        and row['stage'] == 'discovered'
        and row['result'] == {},
        'RETIRE_LEGACY_ROW_CHANGED',
    )
    payload = row['payload']
    need(
        payload.get('video_id') == SOURCE
        and payload.get('url') == 'https://www.youtube.com/watch?v=' + SOURCE,
        'RETIRE_LEGACY_SOURCE_BINDING_CHANGED',
    )
    download = store.get(DOWNLOAD)
    parent = store.get(GROUP)
    need(
        download is not None
        and download['state'] == 'completed'
        and parent is not None
        and parent['state'] == 'completed'
        and parent['source_id'] == SOURCE
        and parent['payload']['download_job_id'] == DOWNLOAD,
        'RETIRE_LEGACY_COMPLETED_SOURCE_REQUIRED',
    )
    children = store.query(
        'SELECT child_job_id,state,external_id FROM replay_part_children_v5 '
        'WHERE parent_job_id=? ORDER BY part_index',
        (GROUP,),
    )
    need(
        len(children) == 2
        and all(child['state'] == 'completed' for child in children)
        and tuple(child['external_id'] for child in children) == PUBLICATIONS,
        'RETIRE_LEGACY_CHILD_STATE_CHANGED',
    )
    publications = store.query(
        'SELECT external_id,state FROM publications WHERE source_platform=? AND source_id=? '
        'ORDER BY external_id',
        ('youtube', SOURCE),
    )
    need(
        sorted((item['external_id'], item['state']) for item in publications)
        == sorted((identifier, 'completed') for identifier in PUBLICATIONS),
        'RETIRE_LEGACY_PUBLICATION_CHANGED',
    )
    other = store.query(
        "SELECT job_id,state FROM jobs WHERE source_platform='youtube' AND source_id=? "
        "AND job_id<>? AND job_type='video_process'",
        (SOURCE, JOB),
    )
    need(
        all(item['state'] in ('completed', 'failed') for item in other),
        'RETIRE_LEGACY_OTHER_PROCESS_ACTIVE',
    )
    return {
        'version': VERSION,
        'source_id': SOURCE,
        'download_job_id': DOWNLOAD,
        'parent_job_id': GROUP,
        'publications': list(PUBLICATIONS),
        'job': dict(row),
        'job_sha256': digest(dict(row)),
        'download_sha256': digest(dict(download)),
        'parent_sha256': digest(dict(parent)),
        'children': [dict(item) for item in children],
        'owner_response': OWNER_RESPONSE,
        'owner_reference': OWNER_REFERENCE,
        'media_moved_or_deleted': False,
        'publications_created': 0,
        'provider_requests': 0,
        'planned_at': now,
    }


def archive_plan(settings, plan, plan_sha):
    folder = settings.root / 'media' / DOWNLOAD / 'legacy-source-process-retirement-v5'
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    need(folder.resolve() == folder and not any(path.is_symlink() for path in (folder, *folder.parents)),
         'RETIRE_LEGACY_ARCHIVE_PATH_INVALID')
    file = folder / (plan_sha + '.json')
    atomic_json(file, plan)
    file.chmod(0o600)
    return file


def retire(store, plan, plan_sha, record_file):
    record = reference(record_file)
    marker = {
        'version': 'youtube-legacy-video-process-superseded-v5',
        'plan_sha256': plan_sha,
        'record': record,
        'old_job_sha256': plan['job_sha256'],
        'source_id': SOURCE,
        'group_id': GROUP,
        'publications': list(PUBLICATIONS),
        'attempts_preserved': 0,
        'media_moved_or_deleted': False,
        'publication_created': False,
        'remote_retry_performed': False,
        'provider_requests': 0,
    }
    old = plan['job']
    result = deepcopy(old['result'])
    result['youtube_legacy_video_process_superseded_v5'] = marker
    now = time.time()
    with store.transaction() as db:
        changed = db.execute(
            """UPDATE jobs SET state='failed',stage='superseded_by_replay_part_group',
               error_code='YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED',result=?,updated_at=?,
               next_run=0,lease_owner=NULL,lease_until=NULL
               WHERE job_id=? AND source_platform='youtube' AND source_id=?
               AND job_type='video_process' AND state='pending' AND attempts=0
               AND payload=? AND result=? AND updated_at=? AND stage='discovered'
               AND error_code IS NULL AND lease_owner IS NULL AND lease_until IS NULL""",
            (
                encode(result), now, JOB, SOURCE, encode(old['payload']),
                encode(old['result']), old['updated_at'],
            ),
        ).rowcount
        need(changed == 1, 'RETIRE_LEGACY_JOB_CAS_FAILED')
        audit = {
            'version': VERSION,
            'plan_sha256': plan_sha,
            'record': record,
            'old_job_sha256': plan['job_sha256'],
            'old_state': 'pending',
            'new_state': 'failed',
            'attempts_preserved': 0,
            'completed_group': GROUP,
            'publications': list(PUBLICATIONS),
            'media_moved_or_deleted': False,
            'publications_created': 0,
            'provider_requests': 0,
        }
        db.execute(
            'INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-authorized-maintenance',
             'youtube_legacy_video_process_superseded_v5', JOB, encode(audit)),
        )
    after = store.get(JOB)
    need(
        after['state'] == 'failed'
        and after['stage'] == 'superseded_by_replay_part_group'
        and after['error_code'] == 'YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED'
        and after['result']['youtube_legacy_video_process_superseded_v5'] == marker,
        'RETIRE_LEGACY_READBACK_FAILED',
    )
    return {
        'state': 'superseded',
        'plan_sha256': plan_sha,
        'record': record,
        'job_id': JOB,
        'attempts_preserved': after['attempts'],
        'publications_preserved': list(PUBLICATIONS),
        'media_moved_or_deleted': False,
        'provider_requests': 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-plan-sha256')
    args = parser.parse_args()
    need(os.getuid() == os.getgid() == 10001, 'RETIRE_LEGACY_APP_UID_REQUIRED')
    from services.hub.config import Settings

    settings = Settings.load()
    need(settings.root == Path('/opt/lighthouse-hub'), 'RETIRE_LEGACY_ROOT_REQUIRED')
    store = Store(settings.db_path)
    plan = current_state(store, time.time())
    plan_sha = stable_sha(plan)
    record_file = archive_plan(settings, plan, plan_sha)
    if args.apply_plan_sha256 is None:
        result = {
            'state': 'verified_legacy_video_process_retirement_plan',
            'plan_sha256': plan_sha,
            'record': reference(record_file),
            'job_id': JOB,
            'attempts': 0,
            'publications': list(PUBLICATIONS),
            'media_moved_or_deleted': False,
            'provider_requests': 0,
        }
    else:
        need(args.apply_plan_sha256 == plan_sha, 'RETIRE_LEGACY_EXACT_PLAN_REQUIRED')
        stored = json.loads(record_file.read_bytes())
        need(stable_sha(stored) == plan_sha, 'RETIRE_LEGACY_RECORD_CHANGED')
        result = retire(store, plan, plan_sha, record_file)
    runs = settings.root / 'media' / DOWNLOAD / 'legacy-source-process-retirement-runs'
    runs.mkdir(parents=True, mode=0o700, exist_ok=True)
    result_file = runs / (str(uuid.uuid4()) + '.json')
    atomic_json(result_file, result)
    result_file.chmod(0o600)
    print(json.dumps({'result_file': str(result_file), **result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
