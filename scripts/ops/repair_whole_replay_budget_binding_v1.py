"""Repair the budget-revision result binding after the guarded worker refusal.

The first revision changed the derived plans and payload but omitted the new
payload digest inside the stored binding. The worker stopped before extracting
or downloading, preserving attempt 3 and the partial. This operator proves the
repair is exactly that one field plus revision metadata, then requeues the same
job without resetting attempts or touching media.
"""
import argparse
from copy import deepcopy
import hashlib
import os
import time

from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.locks import resource_guard
from services.hub.replay_multipart_v4 import digest
from services.hub.youtube_download_v4 import STATE, YouTubeWholeReplayDownload

VERSION = 'youtube-whole-budget-binding-repair-v1'
JOB = 'bca759c3-65f3-4a1f-b949-33181cbc403d'
CANDIDATE = '644bfe85-3005-4b3b-948a-2ba61b8170a4'
PLAN_JOB = '5eaa0889-1613-4149-8699-f94b93cd1ec1'
ERROR = 'YOUTUBE_WHOLE_BINDING_CHANGED'
STORED_BINDING_SHA = 'd32ef9f898f82a021b8e9ce6138988c77f5056053c7884de5ac64cf9f9ccc9d5'
EXPECTED_BINDING_SHA = 'a89f6d15411f5e77105b97c3345438ca1fbd0d517410a394b1dfac8638d8dbce'
EXPECTED_SOURCE_PAYLOAD_SHA = '74622f88f77de3416b399a7b777daf7ced55df3a0e4314a01bb46e6576dcbe3f'
PARTIAL_BYTES = 5_816_720_720


def need(value, code):
    if not value:
        raise RuntimeError(code)


def computed_binding(settings, store):
    job = store.get(JOB)
    service = YouTubeWholeReplayDownload(settings, store)
    # This is a read-only reconstruction; the normal worker fence remains in
    # force for every real claim.
    service.fifo.assert_job_binding = lambda *args, **kwargs: None
    return job, service.resource_binding(job)


def snapshot(settings, store):
    job, expected = computed_binding(settings, store)
    candidates = store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?', (CANDIDATE,))
    need(len(candidates) == 1, 'CANDIDATE_MISSING')
    candidate = candidates[0]
    need(
        job['source_id'] == 'uKXvoszCwek'
        and job['state'] == 'awaiting_review'
        and job['stage'] == 'youtube_whole_acquiring'
        and job['attempts'] == 3
        and job['error_code'] == ERROR
        and job['lease_owner'] is job['lease_until'] is None,
        'JOB_STATE_CHANGED',
    )
    need(
        candidate['state'] == 'hold'
        and candidate['reason'] == ERROR
        and candidate['plan_job_id'] == PLAN_JOB
        and candidate['download_job_id'] == JOB
        and candidate['publication_job_id'] is None,
        'CANDIDATE_STATE_CHANGED',
    )
    state = job['result'].get(STATE)
    need(isinstance(state, dict), 'RESULT_STATE_MISSING')
    stored = deepcopy(state['binding'])
    need(digest(stored) == STORED_BINDING_SHA, 'STORED_BINDING_CHANGED')
    need(digest(expected) == EXPECTED_BINDING_SHA, 'EXPECTED_BINDING_CHANGED')
    repaired = deepcopy(stored)
    repaired['source_payload_sha256'] = expected['source_payload_sha256']
    need(repaired == expected, 'BINDING_DIFF_EXCEEDS_PAYLOAD_DIGEST')
    need(expected['source_payload_sha256'] == EXPECTED_SOURCE_PAYLOAD_SHA == digest(job['payload']),
         'PAYLOAD_DIGEST_CHANGED')

    revision = job['result'].get('download_request_budget_revision_v1')
    need(isinstance(revision, dict), 'BUDGET_REVISION_MISSING')
    need(
        revision['version'] == 'youtube-whole-request-budget-revision-v1'
        and revision['job_id'] == JOB
        and revision['previous_attempts'] == 2
        and revision['new_binding_sha256'] == STORED_BINDING_SHA
        and revision['evidence']['partial']['bytes'] == PARTIAL_BYTES,
        'BUDGET_REVISION_CHANGED',
    )
    area = settings.root / 'media' / JOB / 'youtube-whole-v4'
    partial = area / 'stream-299.mp4.part'
    need(partial.is_file() and not partial.is_symlink() and partial.stat().st_size == PARTIAL_BYTES,
         'PARTIAL_CHANGED')
    calls = store.query(
        "SELECT COUNT(*) n,COALESCE(SUM(units),0) units FROM api_calls "
        "WHERE provider='youtube_download_http' AND job_id=?",
        (JOB,),
    )[0]
    need(calls['n'] == calls['units'] == 1042, 'API_LEDGER_CHANGED')
    free = __import__('shutil').disk_usage(settings.root).free
    floor = int(settings.config['disk_floor_gb'] * 1_000_000_000)
    need(free >= floor, 'DISK_LOW')

    new_state = deepcopy(state)
    new_state['binding'] = repaired
    new_revision = deepcopy(revision)
    new_revision['previous_attempts'] = 3
    new_revision['new_binding_sha256'] = EXPECTED_BINDING_SHA
    new_revision['evidence']['binding_repair'] = {
        'version': VERSION,
        'found_error': ERROR,
        'stored_binding_sha256': STORED_BINDING_SHA,
        'repaired_binding_sha256': EXPECTED_BINDING_SHA,
        'only_field_changed': 'source_payload_sha256',
        'old_source_payload_sha256': stored['source_payload_sha256'],
        'new_source_payload_sha256': expected['source_payload_sha256'],
        'worker_attempt_preserved': 3,
        'partial': {'file': str(partial), 'bytes': partial.stat().st_size, 'content_hash_claimed': False},
        'media_download_performed': False,
    }
    new_result = deepcopy(job['result'])
    new_result[STATE] = new_state
    new_result['download_request_budget_revision_v1'] = new_revision
    return {
        'version': VERSION,
        'job': job,
        'candidate': candidate,
        'stored_binding': stored,
        'repaired_binding': repaired,
        'new_result': new_result,
        'partial_bytes': PARTIAL_BYTES,
        'free_bytes': free,
    }


def plan_digest(value):
    return digest({key: item for key, item in value.items() if key != 'free_bytes'})


def apply(store, value, *, plan_sha256):
    need(plan_digest(value) == plan_sha256, 'PLAN_CHANGED')
    now = time.time()
    with store.transaction() as db:
        need(
            db.execute(
                "UPDATE jobs SET result=?,state='retry_scheduled',error_code=NULL,next_run=?,updated_at=? "
                "WHERE job_id=? AND state='awaiting_review' AND stage='youtube_whole_acquiring' "
                "AND attempts=3 AND error_code=? AND updated_at=? AND result=? "
                "AND lease_owner IS NULL AND lease_until IS NULL",
                (
                    encode(value['new_result']),
                    now,
                    now,
                    JOB,
                    ERROR,
                    value['job']['updated_at'],
                    encode(value['job']['result']),
                ),
            ).rowcount
            == 1,
            'JOB_UPDATE_CONFLICT',
        )
        candidate = value['candidate']
        need(
            db.execute(
                "UPDATE replay_fifo_v4 SET state='download_queued',reason=NULL,updated_at=? "
                "WHERE candidate_id=? AND state='hold' AND reason=? "
                "AND updated_at=? AND plan_job_id=? AND download_job_id=? "
                "AND publication_job_id IS NULL",
                (now, CANDIDATE, ERROR, candidate['updated_at'], PLAN_JOB, JOB),
            ).rowcount
            == 1,
            'CANDIDATE_UPDATE_CONFLICT',
        )
        db.execute(
            'INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (
                now,
                'owner-authorized-maintenance',
                VERSION,
                JOB,
                encode(
                    {
                        'plan_sha256': plan_sha256,
                        'attempts_preserved': 3,
                        'stored_binding_sha256': STORED_BINDING_SHA,
                        'repaired_binding_sha256': EXPECTED_BINDING_SHA,
                        'only_field_changed': 'source_payload_sha256',
                        'partial_bytes': PARTIAL_BYTES,
                        'new_jobs_created': False,
                        'publication_created': False,
                    }
                ),
            ),
        )
    return {
        'state': 'same_job_requeued_after_binding_repair',
        'job_id': JOB,
        'attempts_preserved': 3,
        'repaired_binding_sha256': EXPECTED_BINDING_SHA,
        'partial_bytes': PARTIAL_BYTES,
        'new_jobs_created': False,
        'publication_created': False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-sha256')
    args = parser.parse_args()
    need(os.name == 'posix' and os.getuid() == os.getgid() == 10001 and __debug__,
         'APP_IDENTITY_REQUIRED')
    need(bool(args.apply) == bool(args.plan_sha256), 'EXACT_PLAN_REQUIRED')
    settings = Settings.load()
    store = Store(settings.db_path)
    with resource_guard(settings.root / 'data/locks/video'), resource_guard(
        settings.root / 'data/locks/replay-fifo-dispatch-v4'
    ):
        value = snapshot(settings, store)
        digest_value = plan_digest(value)
        if not args.apply:
            print(encode({
                'state': 'plan_only',
                'version': VERSION,
                'job_id': JOB,
                'attempts_preserved': 3,
                'stored_binding_sha256': STORED_BINDING_SHA,
                'repaired_binding_sha256': EXPECTED_BINDING_SHA,
                'partial_bytes': PARTIAL_BYTES,
                'free_bytes': value['free_bytes'],
                'plan_sha256': digest_value,
                'new_jobs_created': False,
                'publication_created': False,
            }))
            return
        print(encode(apply(store, value, plan_sha256=args.plan_sha256)))


if __name__ == '__main__':
    main()
