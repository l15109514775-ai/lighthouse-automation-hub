"""Restore the two worker attempts consumed by the budget-migration bugs.

Attempt 3 failed before media because the runbook omitted the revised payload
digest from the stored binding. Attempt 4 failed before media because the
validator did not yet allow the derived whole-plan digest to change. Neither
attempt made an HTTP request. This operator verifies both conditions, restores
the original download attempt count 2, and requeues the same candidate.
"""
import argparse
from copy import deepcopy
import os
import time

from adapters import ytdlp_highest_v2 as engine
from adapters.ytdlp_whole_v4 import folder
from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.locks import resource_guard
from services.hub.replay_multipart_v4 import digest
from services.hub.youtube_download_v4 import STATE, YouTubeWholeReplayDownload

VERSION = 'youtube-whole-maintenance-attempt-restore-v1'
JOB = 'bca759c3-65f3-4a1f-b949-33181cbc403d'
CANDIDATE = '644bfe85-3005-4b3b-948a-2ba61b8170a4'
PLAN_JOB = '5eaa0889-1613-4149-8699-f94b93cd1ec1'
ERROR = 'YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID'
EXPECTED_BINDING_SHA = 'a89f6d15411f5e77105b97c3345438ca1fbd0d517410a394b1dfac8638d8dbce'
PARTIAL_BYTES = 5_816_720_720


def need(value, code):
    if not value:
        raise RuntimeError(code)


def snapshot(settings, store):
    job = store.get(JOB)
    candidates = store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?', (CANDIDATE,))
    need(len(candidates) == 1, 'CANDIDATE_MISSING')
    candidate = candidates[0]
    need(
        job['source_id'] == 'uKXvoszCwek'
        and job['state'] == 'awaiting_review'
        and job['stage'] == 'youtube_whole_acquiring'
        and job['attempts'] == 4
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
    service = YouTubeWholeReplayDownload(settings, store)
    service.fifo.assert_job_binding = lambda *args, **kwargs: None
    binding = service.resource_binding(job)
    need(digest(binding) == EXPECTED_BINDING_SHA, 'BINDING_CHANGED')
    revision = engine.request_budget_revision(job, binding)
    need(revision is not None and revision['old_plan']['max_http_requests'] == 1042,
         'BUDGET_REVISION_INVALID')
    carried = engine.resume_http_stats(store, job, binding, folder(settings, JOB))
    need(
        carried['requests'] == 1042
        and carried['request_limit'] == 1628
        and carried['response_kinds']['html'] == 465
        and carried['response_kinds']['media'] == 569,
        'HTTP_HISTORY_CHANGED',
    )
    calls = store.query(
        "SELECT COUNT(*) n,COALESCE(SUM(units),0) units FROM api_calls "
        "WHERE provider='youtube_download_http' AND job_id=?",
        (JOB,),
    )[0]
    need(calls['n'] == calls['units'] == 1042, 'NETWORK_WORK_OCCURRED')
    area = folder(settings, JOB)
    partial = area / 'stream-299.mp4.part'
    need(partial.is_file() and not partial.is_symlink() and partial.stat().st_size == PARTIAL_BYTES,
         'PARTIAL_CHANGED')
    need(not (area / 'stream-140.m4a').exists() and not (area / 'stream-140.m4a.part').exists(),
         'AUDIO_STATE_CHANGED')
    need(not (area / 'original.mp4').exists() and not (area / 'verified.json').exists(),
         'FINAL_STATE_CHANGED')
    result_revision = job['result']['download_request_budget_revision_v1']
    need(
        result_revision['previous_attempts'] == 3
        and result_revision['new_binding_sha256'] == EXPECTED_BINDING_SHA,
        'REVISION_STATE_CHANGED',
    )
    new_revision = deepcopy(result_revision)
    new_revision['previous_attempts'] = 2
    new_revision['evidence']['maintenance_attempt_restore'] = {
        'version': VERSION,
        'attempt_3_error': 'YOUTUBE_WHOLE_BINDING_CHANGED',
        'attempt_4_error': ERROR,
        'both_failed_before_media': True,
        'api_requests_after_failures': 1042,
        'partial_bytes_after_failures': PARTIAL_BYTES,
        'restored_download_attempts': 2,
        'next_worker_attempt': 3,
        'maximum_attempts': 4,
    }
    new_result = deepcopy(job['result'])
    new_result['download_request_budget_revision_v1'] = new_revision
    return {
        'version': VERSION,
        'job': job,
        'candidate': candidate,
        'binding': binding,
        'new_result': new_result,
        'carried_http': carried,
        'partial_bytes': PARTIAL_BYTES,
    }


def apply(store, value, *, plan_sha256):
    need(digest(value) == plan_sha256, 'PLAN_CHANGED')
    now = time.time()
    with store.transaction() as db:
        need(
            db.execute(
                "UPDATE jobs SET result=?,attempts=2,state='retry_scheduled',error_code=NULL,"
                "next_run=?,updated_at=? WHERE job_id=? AND state='awaiting_review' "
                "AND stage='youtube_whole_acquiring' AND attempts=4 AND error_code=? "
                "AND updated_at=? AND result=? AND lease_owner IS NULL AND lease_until IS NULL",
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
                "WHERE candidate_id=? AND state='hold' AND reason=? AND updated_at=? "
                "AND plan_job_id=? AND download_job_id=? AND publication_job_id IS NULL",
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
                        'attempts_before_maintenance_claim': 4,
                        'restored_download_attempts': 2,
                        'maintenance_failures': [
                            'YOUTUBE_WHOLE_BINDING_CHANGED',
                            ERROR,
                        ],
                        'maintenance_failures_preceded_media': True,
                        'api_requests': 1042,
                        'partial_bytes': PARTIAL_BYTES,
                        'binding_sha256': EXPECTED_BINDING_SHA,
                        'new_jobs_created': False,
                        'publication_created': False,
                    }
                ),
            ),
        )
    return {
        'state': 'same_job_requeued_after_maintenance_compensation',
        'job_id': JOB,
        'restored_download_attempts': 2,
        'next_worker_attempt': 3,
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
        digest_value = digest(value)
        if not args.apply:
            print(encode({
                'state': 'plan_only',
                'version': VERSION,
                'job_id': JOB,
                'attempts_before': 4,
                'restore_download_attempts': 2,
                'next_worker_attempt': 3,
                'api_requests': 1042,
                'partial_bytes': PARTIAL_BYTES,
                'carried_http': value['carried_http'],
                'plan_sha256': digest_value,
                'new_jobs_created': False,
                'publication_created': False,
            }))
            return
        print(encode(apply(store, value, plan_sha256=args.plan_sha256)))


if __name__ == '__main__':
    main()
