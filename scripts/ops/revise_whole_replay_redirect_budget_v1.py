"""Revise one held replay request budget after observed redirect overhead.

Default mode is an offline plan. Apply preserves attempts, source identity,
the 5.8GB partial, prior failure evidence and the API ledger. It only changes
the derived request allowance, rewrites the same job's binding/result, creates
hard-link evidence for the prior selection/progress files, and requeues the same
candidate. The worker performs no blind resubmission and validates every input
again after claiming.
"""
from copy import deepcopy
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from adapters.ytdlp_whole_v4 import budget_limits, folder, needed_disk
from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.locks import resource_guard
from services.hub.replay_multipart_v4 import digest, validate_whole_replay_plan
from services.hub.youtube_download_v4 import STATE, YouTubeWholeReplayDownload, transfer_plan

VERSION = 'youtube-whole-redirect-budget-revision-v1'
JOB = 'bca759c3-65f3-4a1f-b949-33181cbc403d'
VIDEO = 'uKXvoszCwek'
CANDIDATE = '644bfe85-3005-4b3b-948a-2ba61b8170a4'
PLAN_JOB = '5eaa0889-1613-4149-8699-f94b93cd1ec1'
ERROR = 'YOUTUBE_HIGHEST_REQUEST_LIMIT'
OLD_SOURCE_PLAN_SHA = '25fc4ec8e6ed23b5ef0ee964eb613a8ceae882f394a3883ebde1d91a03f4f69a'
OLD_BINDING_SHA = 'e507fab3f5c8b23dfbaf985985f2454ee1e6b82347bfec80bf3213ed528bbd04'
OLD_WHOLE_PLAN_SHA = '155c9ab216c9ef6a73eb17b93266fea6be6223fe9f846a3f5dd2e82238d52178'
OLD_PAYLOAD_SHA = '9b8f2761eb9b8c8dedc01a4ee18373b1ac456a2beb8e9e479fdad1c6ffffc246'
OLD_RESULT_STATE_SHA = '8abb1a3042be4b7c23f0c71753e23ae62d1ed75c7760bc00826973d3bb054617'
PARTIAL_BYTES = 5_816_720_720
EXPECTED_HTTP = {
    'budget_history_reset': False,
    'provider': 'youtube_download_http',
    'request_limit': 1042,
    'requests': 1042,
    'response_bytes': 5_830_235_619,
    'response_kinds': {'html': 465, 'javascript': 2, 'json': 4, 'manifest': 2, 'media': 569},
}
SOURCE_SHA = {
    'services/hub/youtube_natural_plan_v3.py':
        'b6fb2c72a0ef1634a46bb31ef3784a751b0655c42a91279c7edca9f9dcc164a5',
    'adapters/ytdlp_highest_v2.py':
        'f37156f0315217d7b909ea5af9daa07cf8d87c714623d8189c465df9b6cb9561',
}


def need(value, code):
    if not value:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    return sha(path.read_bytes())


def descriptor(path, *, maximum=300_000_000, hash_content=True):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)),
         'UNSAFE_FILE')
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink in (1, 2) and 0 < info.st_size <= maximum,
         'FILE_INVALID')
    value = {
        'file': str(path),
        'bytes': info.st_size,
        'device': info.st_dev,
        'inode': info.st_ino,
        'mtime_ns': info.st_mtime_ns,
        'ctime_ns': info.st_ctime_ns,
        'mode': stat.S_IMODE(info.st_mode),
    }
    if hash_content:
        value['sha256'] = file_sha(path)
    else:
        value['content_hash_claimed'] = False
    return value


def json_file(path):
    raw = path.read_bytes()
    return json.loads(raw), sha(raw)


def revise_whole(old):
    new = deepcopy(old)
    basis = new['source_plan_basis']
    media = basis['media_requests_plan']
    metadata = basis['metadata_request_allowance']
    need(basis['redirect_resume_request_allowance'] == max(64, math.ceil(media / 4)),
         'OLD_RESUME_ALLOWANCE_CHANGED')
    need(basis['max_http_requests'] == media + metadata + max(64, math.ceil(media / 4)),
         'OLD_REQUEST_LIMIT_CHANGED')
    basis['redirect_resume_request_allowance'] = media
    basis['max_http_requests'] = media + metadata + media
    return new


def ensure_hardlink(source, target):
    source = Path(source)
    target = Path(target)
    need(not target.is_symlink() and target.parent == source.parent, 'EVIDENCE_PATH_INVALID')
    if target.exists():
        left, right = source.stat(), target.stat()
        need(left.st_dev == right.st_dev and left.st_ino == right.st_ino, 'EVIDENCE_LINK_CHANGED')
        return
    os.link(source, target)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot(settings, store):
    root = settings.root
    need(root.as_posix() == '/opt/lighthouse-hub', 'ROOT_CHANGED')
    for relative, expected in SOURCE_SHA.items():
        need(file_sha(root / relative) == expected, 'QUALIFIED_SOURCE_CHANGED')

    job = store.get(JOB)
    candidates = store.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?', (CANDIDATE,))
    need(len(candidates) == 1, 'CANDIDATE_MISSING')
    candidate = candidates[0]
    need(
        job['source_platform'] == 'youtube'
        and job['source_id'] == VIDEO
        and job['job_type'] == 'youtube_replay_download_v4'
        and job['queue'] == 'video'
        and job['state'] == 'failed'
        and job['stage'] == 'youtube_whole_acquiring'
        and job['attempts'] == 2
        and job['error_code'] == ERROR
        and job['lease_owner'] is job['lease_until'] is None
        and job['external_publish_id'] is None,
        'JOB_STATE_CHANGED',
    )
    need(
        candidate['source_id'] == VIDEO
        and candidate['state'] == 'hold'
        and candidate['reason'] == ERROR
        and candidate['plan_job_id'] == PLAN_JOB
        and candidate['download_job_id'] == JOB
        and candidate['publication_job_id'] is None
        and candidate['refresh_job_id'] is None,
        'CANDIDATE_STATE_CHANGED',
    )
    need(digest(job['payload']) == OLD_PAYLOAD_SHA, 'PAYLOAD_CHANGED')
    state = job['result'].get(STATE)
    need(isinstance(state, dict) and digest(state) == OLD_RESULT_STATE_SHA, 'RESULT_STATE_CHANGED')
    old_binding = state['binding']
    need(digest(old_binding) == OLD_BINDING_SHA, 'OLD_BINDING_CHANGED')
    need(digest(old_binding['source_plan']) == OLD_SOURCE_PLAN_SHA, 'OLD_SOURCE_PLAN_CHANGED')
    need(digest(job['payload']['whole_replay_plan']) == OLD_WHOLE_PLAN_SHA, 'OLD_WHOLE_PLAN_CHANGED')
    need(job['payload']['whole_replay_plan_sha256'] == OLD_WHOLE_PLAN_SHA, 'PAYLOAD_PLAN_BINDING_CHANGED')
    need(job['payload']['fifo_v4']['plan_sha256'] == OLD_WHOLE_PLAN_SHA, 'FIFO_PLAN_BINDING_CHANGED')

    whole = revise_whole(job['payload']['whole_replay_plan'])
    policy = whole['planning_policy']
    validate_whole_replay_plan(whole, policy, official_raw=job['payload']['raw'])
    new_binding = deepcopy(old_binding)
    new_binding['source_plan'] = transfer_plan(whole)
    new_binding['whole_replay_plan'] = whole
    need(new_binding['source_plan']['media_requests_plan'] == 782, 'MEDIA_PLAN_CHANGED')
    need(new_binding['source_plan']['redirect_resume_request_allowance'] == 782, 'NEW_RESUME_ALLOWANCE_CHANGED')
    need(new_binding['source_plan']['max_http_requests'] == 1628, 'NEW_REQUEST_LIMIT_CHANGED')

    new_payload = deepcopy(job['payload'])
    new_payload['whole_replay_plan'] = whole
    new_payload['whole_replay_plan_sha256'] = digest(whole)
    new_payload['fifo_v4']['plan_sha256'] = digest(whole)
    new_state = deepcopy(state)
    new_state['binding'] = new_binding

    area = folder(settings, JOB)
    selection = area / 'selection.json'
    progress = area / 'progress.json'
    failure = area / 'failure-before-attempt-2.json'
    result_file = area / 'download-result.json'
    partial = area / 'stream-299.mp4.part'
    evidence = {
        'selection': descriptor(selection),
        'progress': descriptor(progress),
        'failure_before_attempt_2': descriptor(failure),
        'download_result': descriptor(result_file),
        'partial': descriptor(partial, maximum=PARTIAL_BYTES, hash_content=False),
    }
    selection_value, selection_file_sha = json_file(selection)
    selection_sha = digest(selection_value)
    progress_value, progress_sha = json_file(progress)
    failure_value, failure_sha = json_file(failure)
    result_value, result_sha = json_file(result_file)
    need(evidence['partial']['bytes'] == PARTIAL_BYTES, 'PARTIAL_CHANGED')
    for source, target in (
        (selection, area / 'selection-before-budget-revision.json'),
        (progress, area / 'progress-before-budget-revision.json'),
    ):
        if target.exists():
            left, right = source.stat(), target.stat()
            need(left.st_dev == right.st_dev and left.st_ino == right.st_ino, 'EVIDENCE_LINK_CHANGED')
    need(selection_value == {
        'binding_sha256': OLD_BINDING_SHA,
        'source_plan': old_binding['source_plan'],
        'selected_formats': old_binding['source_plan']['selected_formats'],
    }, 'SELECTION_CHANGED')
    need(progress_value['http'] == EXPECTED_HTTP and progress_value['source_plan'] == old_binding['source_plan'],
         'PROGRESS_CHANGED')
    need(failure_value['http']['requests'] == 6 and failure_value['source_plan'] == old_binding['source_plan'],
         'ATTEMPT_1_CHANGED')
    need(result_value['error_code'] == ERROR and result_value['http'] == EXPECTED_HTTP
         and result_value['source_plan'] == old_binding['source_plan'] and result_value['partial_files_retained'] is True,
         'ATTEMPT_2_CHANGED')
    need(not (area / 'stream-140.m4a').exists() and not (area / 'stream-140.m4a.part').exists(),
         'AUDIO_STATE_CHANGED')
    need(not (area / 'original.mp4').exists() and not (area / 'verified.json').exists(),
         'FINAL_STATE_CHANGED')

    calls = store.query(
        "SELECT * FROM api_calls WHERE provider='youtube_download_http' AND job_id=? ORDER BY started_at,call_id",
        (JOB,),
    )
    need(len(calls) == 1042 and sum(row['units'] for row in calls) == 1042, 'API_LEDGER_CHANGED')
    need(all(row['state'] == 'done' and row['error_code'] is None for row in calls), 'API_LEDGER_STATE_CHANGED')

    remaining = needed_disk(area, new_binding)
    free = __import__('shutil').disk_usage(root).free
    floor = max(policy['disk_floor_bytes'], int(settings.config['disk_floor_gb'] * 1_000_000_000))
    need(free - remaining >= floor, 'DISK_LOW')

    revision = {
        'version': 'youtube-whole-request-budget-revision-v1',
        'job_id': JOB,
        'previous_attempts': 2,
        'old_source_plan': old_binding['source_plan'],
        'new_source_plan': new_binding['source_plan'],
        'old_source_plan_sha256': OLD_SOURCE_PLAN_SHA,
        'new_source_plan_sha256': digest(new_binding['source_plan']),
        'old_binding_sha256': OLD_BINDING_SHA,
        'new_binding_sha256': digest(new_binding),
        'reason': 'observed_html_redirect_overhead',
        'evidence': {
            'selection_sha256': selection_sha,
            'selection_file_sha256': selection_file_sha,
            'progress_sha256': progress_sha,
            'failure_before_attempt_2_sha256': failure_sha,
            'download_result_sha256': result_sha,
            'partial': evidence['partial'],
            'http': EXPECTED_HTTP,
            'redirect_html_observed': 461,
            'media_responses_observed': 569,
            'selection_before_budget_revision': str(area / 'selection-before-budget-revision.json'),
            'progress_before_budget_revision': str(area / 'progress-before-budget-revision.json'),
        },
        'attempts_reset': False,
        'new_jobs_created': False,
        'publication_created': False,
    }
    new_result = deepcopy(job['result'])
    new_result[STATE] = new_state
    new_result['download_request_budget_revision_v1'] = revision
    return {
        'version': VERSION,
        'job': job,
        'candidate': candidate,
        'old_binding': old_binding,
        'new_binding': new_binding,
        'old_payload': job['payload'],
        'new_payload': new_payload,
        'old_result': job['result'],
        'new_result': new_result,
        'revision': revision,
        'evidence': evidence,
        'remaining_working_bytes': remaining,
        'free_bytes': free,
    }


def summary(value):
    return {
        'state': 'plan_only',
        'version': VERSION,
        'job_id': JOB,
        'attempts_preserved': value['job']['attempts'],
        'partial_bytes': value['evidence']['partial']['bytes'],
        'old_source_plan_sha256': OLD_SOURCE_PLAN_SHA,
        'new_source_plan_sha256': digest(value['new_binding']['source_plan']),
        'old_binding_sha256': OLD_BINDING_SHA,
        'new_binding_sha256': digest(value['new_binding']),
        'old_request_limit': 1042,
        'new_request_limit': 1628,
        'observed_redirect_html': 461,
        'observed_media_responses': 569,
        'remaining_working_bytes': value['remaining_working_bytes'],
        'free_bytes': value['free_bytes'],
        'new_jobs_created': False,
        'publication_created': False,
    }


def plan_digest(value):
    """Hash immutable evidence; current free space is checked again at apply."""
    return digest({key: item for key, item in value.items() if key != 'free_bytes'})


def apply(settings, store, value, *, plan_sha256):
    plan = summary(value)
    plan['state'] = 'applying_verified_plan'
    need(plan_digest(value) == plan_sha256, 'PLAN_CHANGED')
    need(digest(store.get(JOB)['payload']) == OLD_PAYLOAD_SHA, 'JOB_CHANGED_BEFORE_APPLY')
    area = folder(settings, JOB)
    ensure_hardlink(area / 'selection.json', area / 'selection-before-budget-revision.json')
    ensure_hardlink(area / 'progress.json', area / 'progress-before-budget-revision.json')
    now = time.time()
    encoded_payload = encode(value['new_payload'])
    encoded_result = encode(value['new_result'])
    with store.transaction() as db:
        need(
            db.execute(
                "UPDATE jobs SET payload=?,result=?,state='retry_scheduled',error_code=NULL,"
                "next_run=?,updated_at=? WHERE job_id=? AND state='failed' AND stage='youtube_whole_acquiring' "
                "AND attempts=2 AND error_code=? AND updated_at=? AND payload=? AND result=? "
                "AND lease_owner IS NULL AND lease_until IS NULL",
                (
                    encoded_payload,
                    encoded_result,
                    now,
                    now,
                    JOB,
                    ERROR,
                    value['job']['updated_at'],
                    encode(value['old_payload']),
                    encode(value['old_result']),
                ),
            ).rowcount
            == 1,
            'JOB_UPDATE_CONFLICT',
        )
        old_candidate = value['candidate']
        need(
            db.execute(
                "UPDATE replay_fifo_v4 SET state='download_queued',reason=NULL,updated_at=? "
                "WHERE candidate_id=? AND state='hold' AND reason=? AND updated_at=? "
                "AND plan_job_id=? AND download_job_id=? AND publication_job_id IS NULL",
                (now, CANDIDATE, ERROR, old_candidate['updated_at'], PLAN_JOB, JOB),
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
                encode({
                    'plan_sha256': plan_sha256,
                    'old_binding_sha256': OLD_BINDING_SHA,
                    'new_binding_sha256': digest(value['new_binding']),
                    'old_payload_sha256': OLD_PAYLOAD_SHA,
                    'new_payload_sha256': digest(value['new_payload']),
                    'old_result_sha256': digest(value['old_result']),
                    'new_result_sha256': digest(value['new_result']),
                    'partial': value['evidence']['partial'],
                    'evidence': value['revision']['evidence'],
                    'attempts_reset': False,
                    'new_jobs_created': False,
                    'publication_created': False,
                }),
            ),
        )
    return {
        'state': 'same_download_requeued_with_revised_bound',
        'job_id': JOB,
        'candidate_id': CANDIDATE,
        'attempts_preserved': 2,
        'old_request_limit': 1042,
        'new_request_limit': 1628,
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
    settings, store = Settings.load(), None
    store = Store(settings.db_path)
    with resource_guard(settings.root / 'data/locks/video'), resource_guard(
        settings.root / 'data/locks/replay-fifo-dispatch-v4'
    ):
        value = snapshot(settings, store)
        plan_sha = plan_digest(value)
        if not args.apply:
            print(encode({**summary(value), 'plan_sha256': plan_sha}))
            return
        print(encode(apply(settings, store, value, plan_sha256=args.plan_sha256)))


if __name__ == '__main__':
    main()
