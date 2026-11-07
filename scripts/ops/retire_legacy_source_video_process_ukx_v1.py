"""Retire the stale uKX source-level video_process row after V5 publication.

The first invocation is a read-only exact plan. It preserves the legacy row's
payload and attempts, writes no publication and does not touch media.
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
JOB = '385f8616-335d-4dcf-be2f-32dca07426bb'
SOURCE = 'exampleVid6'
DOWNLOAD = 'bca759c3-65f3-4a1f-b949-33181cbc403d'
GROUP = '3424c0f5-41fd-4588-86f8-a797dfb00d1b'
PUBLICATION = 'BV1MExD6WEKG'
OWNER_RESPONSE = '帮我看看有没有没删掉的历史文件、旧视频之类的。帮我删了。'
OWNER_REFERENCE = 'chat:2026-11-04:cleanup-history-old-videos'


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def plan_hash(plan):
    value = deepcopy(plan)
    value.pop('planned_at', None)
    return digest(value)


def reference(file):
    file = Path(file)
    need(file.is_absolute() and file.resolve() == file and file.is_file()
         and not any(path.is_symlink() for path in (file, *file.parents)), 'RETIRE_UKX_PATH_INVALID')
    raw = file.read_bytes()
    need(0 < len(raw) <= 2_000_000, 'RETIRE_UKX_RECORD_SIZE_INVALID')
    return {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def snapshot(store, now):
    job = store.get(JOB)
    need(job is not None, 'RETIRE_UKX_JOB_MISSING')
    need(
        job['source_platform'] == 'youtube'
        and job['source_id'] == SOURCE
        and job['job_type'] == 'video_process'
        and job['policy_version'] == 'machine-tweet-v3-api'
        and job['queue'] == 'video'
        and job['state'] == 'pending'
        and job['attempts'] == 0
        and job['error_code'] is None
        and job['external_publish_id'] is None
        and job['lease_owner'] is None
        and job['lease_until'] is None
        and job['next_run'] == 0
        and job['stage'] == 'discovered'
        and job['result'] == {},
        'RETIRE_UKX_ROW_CHANGED',
    )
    need(
        job['payload'].get('video_id') == SOURCE
        and job['payload'].get('url') == 'https://www.youtube.com/watch?v=' + SOURCE,
        'RETIRE_UKX_SOURCE_BINDING_CHANGED',
    )
    download = store.get(DOWNLOAD)
    group = store.get(GROUP)
    need(
        download is not None and download['state'] == 'completed'
        and group is not None and group['state'] == 'completed'
        and group['source_id'] == SOURCE
        and group['payload']['download_job_id'] == DOWNLOAD
        and group['payload']['part_count'] == 1,
        'RETIRE_UKX_COMPLETED_SOURCE_REQUIRED',
    )
    children = store.query(
        'SELECT child_job_id,part_index,state,external_id FROM replay_part_children_v5 '
        'WHERE parent_job_id=? ORDER BY part_index',
        (GROUP,),
    )
    need(
        len(children) == 1
        and children[0]['part_index'] == 0
        and children[0]['state'] == 'completed'
        and children[0]['external_id'] == PUBLICATION,
        'RETIRE_UKX_CHILD_STATE_CHANGED',
    )
    publications = store.query(
        "SELECT external_id,state FROM publications WHERE source_platform='youtube' AND source_id=?",
        (SOURCE,),
    )
    need(
        len(publications) == 1
        and publications[0]['external_id'] == PUBLICATION
        and publications[0]['state'] == 'completed',
        'RETIRE_UKX_PUBLICATION_CHANGED',
    )
    other = store.query(
        "SELECT job_id,state FROM jobs WHERE source_platform='youtube' AND source_id=? "
        "AND job_id<>? AND job_type='video_process'",
        (SOURCE, JOB),
    )
    need(all(item['state'] in ('completed', 'failed') for item in other),
         'RETIRE_UKX_OTHER_PROCESS_ACTIVE')
    return {
        'version': VERSION,
        'source_id': SOURCE,
        'download_job_id': DOWNLOAD,
        'parent_job_id': GROUP,
        'publication': PUBLICATION,
        'job': dict(job),
        'job_sha256': digest(dict(job)),
        'download_sha256': digest(dict(download)),
        'group_sha256': digest(dict(group)),
        'children': [dict(item) for item in children],
        'owner_response': OWNER_RESPONSE,
        'owner_reference': OWNER_REFERENCE,
        'media_moved_or_deleted': False,
        'publications_created': 0,
        'provider_requests': 0,
        'planned_at': now,
    }


def record(settings, plan, expected):
    folder = settings.root / 'media' / DOWNLOAD / 'legacy-source-process-retirement-v5'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(folder.resolve() == folder and not any(path.is_symlink() for path in (folder, *folder.parents)),
         'RETIRE_UKX_ARCHIVE_PATH_INVALID')
    file = folder / (expected + '.json')
    atomic_json(file, plan)
    file.chmod(0o600)
    return file


def apply(store, plan, expected, record_file):
    need(plan_hash(plan) == expected, 'RETIRE_UKX_PLAN_CHANGED')
    ref = reference(record_file)
    marker = {
        'version': 'youtube-legacy-video-process-superseded-v5',
        'plan_sha256': expected,
        'record': ref,
        'old_job_sha256': plan['job_sha256'],
        'source_id': SOURCE,
        'group_id': GROUP,
        'publications': [PUBLICATION],
        'attempts_preserved': 0,
        'media_moved_or_deleted': False,
        'publication_created': False,
        'remote_retry_performed': False,
        'provider_requests': 0,
    }
    old = plan['job']
    result = {**deepcopy(old['result']), 'youtube_legacy_video_process_superseded_v5': marker}
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
        need(changed == 1, 'RETIRE_UKX_JOB_CAS_FAILED')
        audit = {
            'version': VERSION,
            'plan_sha256': expected,
            'record': ref,
            'old_job_sha256': plan['job_sha256'],
            'old_state': 'pending',
            'new_state': 'failed',
            'attempts_preserved': 0,
            'completed_group': GROUP,
            'publications': [PUBLICATION],
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
        'RETIRE_UKX_READBACK_FAILED',
    )
    return {
        'state': 'superseded',
        'plan_sha256': expected,
        'job_id': JOB,
        'attempts_preserved': 0,
        'publications_preserved': [PUBLICATION],
        'media_moved_or_deleted': False,
        'provider_requests': 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply-plan-sha256')
    args = parser.parse_args()
    need(os.getuid() == os.getgid() == 10001, 'RETIRE_UKX_APP_UID_REQUIRED')
    from services.hub.config import Settings

    settings = Settings.load()
    store = Store(settings.db_path)
    plan = snapshot(store, time.time())
    expected = plan_hash(plan)
    record_file = record(settings, plan, expected)
    if args.apply_plan_sha256 is None:
        result = {
            'state': 'readonly_plan',
            'plan_sha256': expected,
            'record': reference(record_file),
            'job_id': JOB,
            'attempts': 0,
            'publication': PUBLICATION,
            'media_moved_or_deleted': False,
            'provider_requests': 0,
        }
    else:
        need(args.apply_plan_sha256 == expected, 'RETIRE_UKX_EXACT_PLAN_REQUIRED')
        saved = json.loads(record_file.read_bytes())
        need(plan_hash(saved) == expected, 'RETIRE_UKX_RECORD_CHANGED')
        result = apply(store, plan, expected, record_file)
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
