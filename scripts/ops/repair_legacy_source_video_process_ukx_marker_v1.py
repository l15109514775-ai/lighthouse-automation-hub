"""Repair the retirement marker to the canonical legacy-retirement contract."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import time

from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.media_processing import atomic_json

VERSION = 'retire-legacy-source-video-process-v5'
JOB = '385f8616-335d-4dcf-be2f-32dca07426bb'
SOURCE = 'exampleVid6'
DOWNLOAD = 'bca759c3-65f3-4a1f-b949-33181cbc403d'
GROUP = '3424c0f5-41fd-4588-86f8-a797dfb00d1b'
PUBLICATION = 'BV1MExD6WEKG'


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def stable_sha(plan):
    value = dict(plan)
    value.pop('planned_at', None)
    return digest(value)


def reference(file):
    raw = file.read_bytes()
    need(0 < len(raw) <= 2_000_000, 'REPAIR_UKX_RECORD_INVALID')
    return {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def snapshot(settings, store):
    job = store.get(JOB)
    need(
        job is not None and job['state'] == 'failed'
        and job['stage'] == 'superseded_by_replay_part_group'
        and job['error_code'] == 'YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED'
        and job['attempts'] == 0 and job['lease_owner'] is None and job['lease_until'] is None,
        'REPAIR_UKX_JOB_CHANGED',
    )
    marker = job['result']['youtube_legacy_video_process_superseded_v5']
    need(
        marker['version'] == 'youtube-legacy-video-process-superseded-v5'
        and marker['source_id'] == SOURCE
        and marker['group_id'] == GROUP
        and marker['publications'] == [PUBLICATION]
        and marker['remote_retry_performed'] is False
        and marker['publication_created'] is False,
        'REPAIR_UKX_MARKER_CHANGED',
    )
    old_record = Path(marker['record']['file'])
    need(old_record.parent == settings.root / 'media' / DOWNLOAD / 'legacy-source-process-retirement-v5',
         'REPAIR_UKX_RECORD_PATH_CHANGED')
    old_raw = old_record.read_bytes()
    need(reference(old_record)['sha256'] == marker['record']['sha256'], 'REPAIR_UKX_RECORD_HASH_CHANGED')
    old_plan = json.loads(old_raw)
    need(
        old_plan['version'] == 'retire-legacy-ukx-source-video-process-v1'
        and old_plan['source_id'] == SOURCE
        and old_plan['download_job_id'] == DOWNLOAD
        and old_plan['parent_job_id'] == GROUP
        and old_plan['job']['job_id'] == JOB
        and digest(old_plan['job']) == marker['old_job_sha256'],
        'REPAIR_UKX_OLD_PLAN_CHANGED',
    )
    corrected = {**old_plan, 'version': VERSION}
    corrected_sha = stable_sha(corrected)
    corrected_file = old_record.parent / (corrected_sha + '.json')
    if not corrected_file.exists():
        atomic_json(corrected_file, corrected)
        corrected_file.chmod(0o600)
    saved = json.loads(corrected_file.read_bytes())
    need(stable_sha(saved) == corrected_sha and saved == corrected, 'REPAIR_UKX_NEW_RECORD_CHANGED')
    ref = reference(corrected_file)
    new_marker = {
        **marker,
        'plan_sha256': corrected_sha,
        'record': ref,
    }
    return {
        'job': job,
        'old_marker': marker,
        'new_marker': new_marker,
        'record': ref,
        'plan_sha256': corrected_sha,
    }


def apply(store, value, expected):
    need(digest(value) == expected, 'REPAIR_UKX_PLAN_CHANGED')
    now = time.time()
    old = value['job']
    result = {**old['result'], 'youtube_legacy_video_process_superseded_v5': value['new_marker']}
    with store.transaction() as db:
        need(
            db.execute(
                "UPDATE jobs SET result=?,updated_at=? WHERE job_id=? AND state='failed' "
                "AND stage='superseded_by_replay_part_group' "
                "AND error_code='YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED' "
                "AND result=? AND attempts=0 AND lease_owner IS NULL AND lease_until IS NULL",
                (encode(result), now, JOB, encode(old['result'])),
            ).rowcount
            == 1,
            'REPAIR_UKX_JOB_CAS_FAILED',
        )
        outcome = {
            'version': 'repair-legacy-ukx-marker-v1',
            'plan_sha256': value['plan_sha256'],
            'record': value['record'],
            'old_job_sha256': value['old_marker']['old_job_sha256'],
            'old_marker_sha256': digest(value['old_marker']),
            'new_marker_sha256': digest(value['new_marker']),
            'media_moved_or_deleted': False,
            'provider_requests': 0,
        }
        db.execute(
            'INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-authorized-maintenance',
             'youtube_legacy_video_process_superseded_v5', JOB, encode(outcome)),
        )
    return {
        'state': 'marker_repaired',
        'job_id': JOB,
        'plan_sha256': value['plan_sha256'],
        'record': value['record'],
        'media_moved_or_deleted': False,
        'provider_requests': 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply-plan-sha256')
    args = parser.parse_args()
    need(os.getuid() == os.getgid() == 10001, 'REPAIR_UKX_APP_UID_REQUIRED')
    settings = Settings.load()
    store = Store(settings.db_path)
    value = snapshot(settings, store)
    expected = digest(value)
    if args.apply_plan_sha256 is None:
        print(json.dumps({
            'state': 'readonly_plan',
            'plan_sha256': expected,
            'job_id': JOB,
            'record': value['record'],
            'media_moved_or_deleted': False,
            'provider_requests': 0,
        }, ensure_ascii=False, separators=(',', ':')))
    else:
        need(args.apply_plan_sha256 == expected, 'REPAIR_UKX_EXACT_PLAN_REQUIRED')
        print(json.dumps(apply(store, value, expected), ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
