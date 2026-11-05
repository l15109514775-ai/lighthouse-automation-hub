"""Resume one whole-replay download that a worker crash left terminally failed.

``Worker`` records any unexpected exception as ``failed`` even while attempts
remain, so a process-level crash (for example a fatal background-thread
``OperationalError``) stops an otherwise healthy download for good. This operator
requeues that exact job without resetting attempts or touching media: the download
resumes from its retained partial and progress checkpoint.

It refuses anything that is not a crashed, unverified, unpublished download.
"""
import argparse
import json
import os
import sqlite3
import time

from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.locks import resource_guard
from services.hub.replay_fifo_v4 import DOWNLOAD_JOB, PLAN_JOB, VERSION as FIFO_VERSION

VERSION = 'resume-failed-replay-download-v1'
ERROR = 'UNEXPECTED_RUNTIME_ERROR'
JOB = '424f0360-f244-445e-af3e-c90be21b6ef4'
CANDIDATE = '05bd6fef-e1f9-4560-83f7-68c1c343f10d'
PLAN_JOB = 'ee504a5a-7342-4252-ba2c-f6355e91dae1'


def need(value, code):
    if not value:
        raise RuntimeError(code)


def inspect(settings, store):
    need(settings.root.as_posix() == '/opt/lighthouse-hub', 'ROOT_CHANGED')
    jobs = store.query('SELECT * FROM jobs WHERE job_id=?', (JOB,))
    candidates = store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?', (CANDIDATE,))
    need(len(jobs) == len(candidates) == 1, 'TARGET_ROW_MISSING')
    job, candidate = jobs[0], candidates[0]
    need((job['job_type'], job['queue'], job['state'], job['error_code'], job['policy_version']) ==
         (DOWNLOAD_JOB, 'video', 'failed', ERROR, FIFO_VERSION), 'JOB_STATE_CHANGED')
    need(job['attempts'] < settings.config['max_attempts'], 'ATTEMPTS_EXHAUSTED')
    need(job['lease_owner'] is None and job['lease_until'] is None, 'JOB_LEASED')
    need((candidate['source_id'], candidate['state'], candidate['reason'], candidate['plan_job_id'],
          candidate['download_job_id']) == (job['source_id'], 'hold', ERROR, PLAN_JOB, JOB),
         'CANDIDATE_STATE_CHANGED')
    need(candidate['publication_job_id'] is None, 'PUBLICATION_PRESENT')
    plan = store.query('SELECT state,result FROM jobs WHERE job_id=?', (PLAN_JOB,))
    need(len(plan) == 1 and plan[0]['state'] == 'completed' and
         json.loads(plan[0]['result']).get('replay_fifo_plan_v4', {}).get('candidate_id') == CANDIDATE,
         'PLAN_BINDING_CHANGED')
    need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",
                         (job['source_id'],)), 'PUBLICATION_PRESENT')
    area = settings.root / 'media' / JOB / 'youtube-whole-v4'
    partials = sorted(part.stat().st_size for part in area.glob('*.part')) if area.is_dir() else []
    need(partials and min(partials) > 0, 'PARTIAL_MISSING')
    need(not (area / 'verified.json').exists() and not (area / 'original.mp4').exists(), 'MEDIA_ALREADY_VERIFIED')
    return {'job': job, 'candidate': candidate, 'partial_bytes': partials, 'plan_job_id': PLAN_JOB}


def apply(store, value, *, now, maintenance_reference):
    job, candidate = value['job'], value['candidate']
    with store.transaction() as db:
        changed = db.execute("""UPDATE jobs SET state='retry_scheduled',error_code=NULL,next_run=?,updated_at=?
            WHERE job_id=? AND state='failed' AND error_code=? AND attempts=? AND updated_at=?
            AND lease_owner IS NULL AND lease_until IS NULL""",
            (now, now, job['job_id'], ERROR, job['attempts'], job['updated_at'])).rowcount
        need(changed == 1, 'JOB_UPDATE_CONFLICT')
        changed = db.execute("""UPDATE replay_fifo_v4 SET state='download_queued',reason=NULL,updated_at=?
            WHERE candidate_id=? AND state='hold' AND reason=? AND updated_at=? AND download_job_id=?""",
            (now, candidate['candidate_id'], ERROR, candidate['updated_at'], job['job_id'])).rowcount
        need(changed == 1, 'CANDIDATE_UPDATE_CONFLICT')
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-authorized-maintenance', VERSION, job['job_id'], encode({
                'candidate_id': candidate['candidate_id'], 'previous_error': ERROR,
                'previous_state': 'failed', 'attempts_preserved': job['attempts'],
                'next_worker_attempt': job['attempts'] + 1, 'partial_bytes': value['partial_bytes'],
                'maintenance_reference': maintenance_reference, 'media_deleted': False,
                'publication_created': False})))
    return {'state': 'download_requeued', 'job_id': job['job_id'], 'attempts_preserved': job['attempts'],
            'next_worker_attempt': job['attempts'] + 1, 'partial_bytes': value['partial_bytes'],
            'media_deleted': False, 'publication_created': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--maintenance-reference', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    need(os.name == 'posix' and os.getuid() == 10001, 'APP_IDENTITY_REQUIRED')
    need(isinstance(args.maintenance_reference, str) and 0 < len(args.maintenance_reference.strip()) <= 2048,
         'MAINTENANCE_REFERENCE_REQUIRED')
    settings = Settings.load()
    store = Store(settings.db_path)
    with resource_guard(settings.root / 'data/locks/video'), resource_guard(
            settings.root / 'data/locks/replay-fifo-dispatch-v4'):
        value = inspect(settings, store)
        if not args.apply:
            print(encode({'state': 'plan_only', 'job_id': JOB, 'attempts': value['job']['attempts'],
                          'next_worker_attempt': value['job']['attempts'] + 1,
                          'partial_bytes': value['partial_bytes'], 'media_deleted': False}))
            return
        now = time.time()
        print(encode(apply(store, value, now=now, maintenance_reference=args.maintenance_reference)))


if __name__ == '__main__':
    main()
