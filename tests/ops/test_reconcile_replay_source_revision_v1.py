"""Owned real SQLite/readiness fixtures for the recurring source-revision pass.

The plan job, readiness wait and failed observation child are produced by the real
V4 coordinator; only the extractor/transport seam is owned. The reconciler runs
against that genuine state and must preserve attempts, rows and audit.
"""
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import sys
import time

import pytest

from services.hub.db import encode
from services.hub.replay_fifo_v4 import ReplayFIFOV4
from services.hub.worker import Worker
from services.hub.youtube_readiness_v4 import JOB_TYPE, WholeReadinessObservationV4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_replay_fifo_v4 import fifo  # noqa: E402,F401  - fixture dependency of readiness
from test_youtube_readiness_v4 import begin, readiness  # noqa: E402

FILE = Path(__file__).resolve().parents[2] / 'scripts/ops/reconcile_replay_source_revision_v1.py'
SPEC = importlib.util.spec_from_file_location('owned_source_revision_reconcile', FILE)
op = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(op)

REFERENCE = 'owned recurring post-processing reconciliation'


def prepare(f, monkeypatch):
    monkeypatch.setattr(op, 'runtime_allowed', lambda: True)
    monkeypatch.setattr(op.Settings, 'load', staticmethod(lambda: f.s))
    monkeypatch.setattr('adapters.ytdlp_whole_v4.WholeMaintenance.check_disk', lambda self, reserve=0: 10 ** 15)
    monkeypatch.setattr('adapters.ytdlp_highest_v2.preflight_budget', lambda *a, **k: None)


def register(f, upload='uploaded'):
    raw = deepcopy(f.raw)
    raw['status']['uploadStatus'] = upload
    f.raw = raw
    f.q.initialize()
    f.coordinator.initialize()
    with f.db.transaction() as db:
        db.execute('DELETE FROM youtube_readiness_waits_v4')
        db.execute('DELETE FROM replay_fifo_dispatches_v4')
        db.execute('DELETE FROM replay_fifo_v4')
        db.execute('DELETE FROM jobs')
        db.execute('DELETE FROM audit')
        db.execute("""UPDATE youtube_videos SET metadata=?,published_at=?,duration=?,actual_start=?,actual_end=?,
            last_api_check=? WHERE video_id=?""",
            (encode(raw), raw['snippet']['publishedAt'], raw['contentDetails']['duration'],
             raw['liveStreamingDetails']['actualStartTime'], raw['liveStreamingDetails']['actualEndTime'],
             f.clock[0], raw['id']))
    registered = f.q.register_all_observed()['registered']
    assert len(registered) == 1
    return registered[0]


def publish_revision(f, *, duration='PT3591S', upload='processed', actual_end=None):
    raw = deepcopy(f.raw)
    raw['contentDetails']['duration'] = duration
    raw['contentDetails']['definition'] = 'hd'
    raw['snippet']['publishedAt'] = '2026-11-04T17:08:23Z'
    raw['status']['uploadStatus'] = upload
    raw['etag'] = 'owned-post-processing-etag'
    if actual_end is not None:
        raw['liveStreamingDetails']['actualEndTime'] = actual_end
    with f.db.transaction() as db:
        db.execute("""UPDATE youtube_videos SET metadata=?,published_at=?,duration=?,actual_start=?,actual_end=?,
            last_api_check=? WHERE video_id=?""",
            (encode(raw), raw['snippet']['publishedAt'], raw['contentDetails']['duration'],
             raw['liveStreamingDetails']['actualStartTime'], raw['liveStreamingDetails']['actualEndTime'],
             f.clock[0], raw['id']))
    return raw


def run(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, 'argv', [str(FILE), '--maintenance-reference', REFERENCE, *args])
    try:
        op.main()
        code = 0
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1]), code


def test_held_readiness_wait_is_reconciled_once_and_the_coordinator_resumes(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    f.parent_id = f.q.enqueue_plan_tick(f.handlers)['created'][0]
    begin(f)
    with f.db.transaction() as db:
        db.execute('UPDATE youtube_readiness_waits_v4 SET next_check=?', (f.clock[0],))
    child = f.coordinator.tick()['enqueued'][0]
    publish_revision(f)
    service = WholeReadinessObservationV4(f.s, f.db, coordinator=f.coordinator, downloader_factory=f.factory)
    assert Worker(f.s, f.db, {JOB_TYPE: service.process}).once('video')
    assert f.coordinator.tick()['held'][0]['code'] == op.ERROR
    before = f.db.get(f.parent_id)
    plan, code = run(monkeypatch, capsys)
    assert code == 0 and plan['approvals'] and plan['approvals'][0]['tier'] == 'post_processing', plan.get('skipped')
    assert f.db.get(f.parent_id) == before
    result, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and len(result['rebased']) == 1 and result['rebased'][0]['candidate_state'] == 'waiting_readiness'
    job = f.db.get(f.parent_id)
    candidate = f.db.query('SELECT * FROM replay_fifo_v4')[0]
    wait = f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert (job['job_id'], job['attempts'], job['state'], job['error_code']) == \
        (before['job_id'], 1, 'retry_scheduled', 'WAITING_YOUTUBE_READINESS')
    assert job['payload']['raw']['contentDetails']['duration'] == 'PT3591S'
    assert candidate['state'] == 'waiting_readiness' and candidate['reason'] is None
    assert wait['state'] == 'waiting_readiness' and wait['active_child_id'] is None
    assert json.loads(wait['record'])['previous_hold']['child_job_id'] == child
    assert f.db.get(child)['state'] == 'awaiting_review'
    assert not f.db.query('SELECT * FROM publications')
    f.clock[0] = time.time() + 1
    resumed = f.coordinator.tick()['enqueued']
    assert len(resumed) == 1 and f.db.get(resumed[0])['source_id'] == f.raw['id'] + ':readiness:3'
    assert f.db.get(f.parent_id)['attempts'] == 1


def test_hold_before_any_plan_is_reconciled_to_registered_and_planned_again(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    publish_revision(f)
    assert f.q.enqueue_plan_tick(f.handlers)['state'] == 'hold'
    result, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and result['rebased'][0]['kind'] == 'none', result.get('errors')
    assert f.db.query('SELECT state,reason FROM replay_fifo_v4')[0] == {'state': 'registered', 'reason': None}
    made = f.q.enqueue_plan_tick(f.handlers)
    assert len(made['created']) == 1
    job = f.db.get(made['created'][0])
    assert job['payload']['raw']['contentDetails']['duration'] == 'PT3591S' and job['state'] == 'pending'


def test_finalized_shrink_is_automatic_but_growth_requires_approval(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f, upload='processed')
    publish_revision(f, duration='PT3591S')
    assert f.q.enqueue_plan_tick(f.handlers)['state'] == 'hold'
    result, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and result['rebased'][0]['kind'] == 'none'
    assert f.db.query('SELECT state FROM replay_fifo_v4')[0]['state'] == 'registered'
    prepare(f, monkeypatch)
    register(f, upload='processed')
    publish_revision(f, duration='PT3630S')
    assert f.q.enqueue_plan_tick(f.handlers)['state'] == 'hold'
    out, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and out['rebased'] == [] and out['approvals'][0]['reason'] == 'GROWTH_REQUIRES_APPROVAL'
    assert f.db.query('SELECT state,reason FROM replay_fifo_v4')[0] == {'state': 'hold', 'reason': op.ERROR}
    package = json.loads(Path(out['approvals'][0]['package']['path']).read_text('utf-8'))
    assert package['approve_command'] and package['evidence']['duration_delta_seconds'] == 30
    assert f.db.query("SELECT 1 FROM events WHERE name='review.required'")


def test_approval_tier_never_mutates_until_explicitly_approved(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    publish_revision(f, duration='PT3400S')
    assert f.q.enqueue_plan_tick(f.handlers)['state'] == 'hold'
    out, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and out['approvals'][0]['reason'] == 'APPROVAL_REQUIRED'
    assert f.db.query('SELECT state,reason FROM replay_fifo_v4')[0] == {'state': 'hold', 'reason': op.ERROR}
    assert not f.db.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))
    candidate_id = f.db.query('SELECT candidate_id FROM replay_fifo_v4')[0]['candidate_id']
    approved, code = run(monkeypatch, capsys, '--approve', candidate_id)
    assert code == 0 and approved['rebased'][0]['kind'] == 'none'
    assert f.db.query('SELECT state,reason FROM replay_fifo_v4')[0] == {'state': 'registered', 'reason': None}
    assert len(f.db.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))) == 1


def test_identity_change_publication_and_large_revision_hold_with_packages(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    publish_revision(f, actual_end='2026-11-01T02:02:00Z')
    assert f.q.enqueue_plan_tick(f.handlers)['state'] == 'hold'
    out, code = run(monkeypatch, capsys)
    assert out['held'][0]['reason'] == 'STABLE_IDENTITY_CHANGED'
    publish_revision(f, duration='PT2000S')
    out, code = run(monkeypatch, capsys)
    assert out['held'][0]['reason'] == 'LARGE_REVISION'
    publish_revision(f, duration='PT3591S')
    uid = ReplayFIFOV4(f.s, f.db).policy()['target_site_uid']
    with f.db.transaction() as db:
        db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
            policy_version,payload) VALUES('owned-job','youtube',?,'owned','video','completed',?,?,'owned','{}')""",
            (f.raw['id'], time.time(), time.time()))
        db.execute("""INSERT INTO publications(idempotency_key,job_id,platform,source_platform,source_id,
            account_id,kind,state,updated_at) VALUES('owned-key','owned-job','bilibili','youtube',?,?,'owned','unknown',?)""",
            (f.raw['id'], 'site_uid:' + uid, time.time()))
    out, code = run(monkeypatch, capsys)
    assert out['held'][0]['reason'] == 'PUBLICATION_PRESENT'
    assert not f.db.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))


def test_stable_unfinalised_observation_becomes_eligible_after_three_runs(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    publish_revision(f, upload='uploaded', duration='PT3591S')
    assert f.q.enqueue_plan_tick(f.handlers)['state'] == 'hold'
    first, code = run(monkeypatch, capsys)
    assert first['held'][0]['reason'] == 'UNSUPPORTED_TRANSITION'
    second, code = run(monkeypatch, capsys)
    assert second['held'][0]['reason'] == 'UNSUPPORTED_TRANSITION'
    third, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and third['rebased'] and third['rebased'][0]['kind'] == 'none'
    assert f.db.query('SELECT state FROM replay_fifo_v4')[0]['state'] == 'registered'


def test_crashed_download_is_requeued_without_resetting_attempts(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    now = time.time()
    candidate_id = f.db.query('SELECT candidate_id FROM replay_fifo_v4')[0]['candidate_id']
    partial = f.s.root / 'media' / 'owned-download' / 'youtube-whole-v4' / 'stream-299.mp4.part'
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b'x' * 4096)
    with f.db.transaction() as db:
        db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
            policy_version,payload,result) VALUES('owned-plan','youtube',?,'youtube_replay_plan_v4','video','completed',
            ?,?,'replay-fifo-v4','{}',?)""", (f.raw['id'], now, now,
            encode({'replay_fifo_plan_v4': {'candidate_id': candidate_id}})))
        db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
            policy_version,payload) VALUES('owned-download','youtube',?,'youtube_replay_download_v4','video','failed',
            ?,?,'replay-fifo-v4','{}')""", (f.raw['id'], now, now))
        db.execute("""UPDATE jobs SET attempts=2,error_code='UNEXPECTED_RUNTIME_ERROR',stage='youtube_whole_acquiring'
            WHERE job_id='owned-download'""")
        db.execute("""UPDATE replay_fifo_v4 SET plan_job_id='owned-plan',download_job_id='owned-download',
            state='hold',reason='UNEXPECTED_RUNTIME_ERROR'""")
    result, code = run(monkeypatch, capsys, '--apply')
    assert code == 0 and result['rebased'][0]['kind'] == 'resume_download'
    job = f.db.get('owned-download')
    assert (job['state'], job['error_code'], job['attempts']) == ('retry_scheduled', None, 2)
    assert f.db.query('SELECT state,reason FROM replay_fifo_v4')[0] == {'state': 'download_queued', 'reason': None}
    audit = f.db.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))
    assert len(audit) == 1 and audit[0]['target'] == 'owned-download'
    assert partial.read_bytes() == b'x' * 4096


def test_failed_media_less_download_is_retired_and_the_plan_reissued(readiness, monkeypatch, capsys):
    f = readiness
    prepare(f, monkeypatch)
    register(f)
    candidate = f.db.query('SELECT * FROM replay_fifo_v4')[0]
    cid, source = candidate['candidate_id'], f.raw['id']
    payload = json.loads(candidate['source_payload'])
    payload['fifo_v4'] = {'version': 'replay-fifo-v4', 'candidate_id': cid,
        'policy_sha256': candidate['policy_sha256'], 'identity_sha256': candidate['identity_sha256'],
        'source_metadata_sha256': candidate['source_metadata_sha256']}
    result = {'replay_fifo_plan_v4': {'candidate_id': cid, 'state': 'planned', 'plan_sha256': 'a' * 64, 'plan': {}}}
    now = time.time()
    partial = f.s.root / 'media' / 'owned-download' / 'youtube-whole-v4' / 'stream-299.mp4.part'
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b'x' * 4096)
    with f.db.transaction() as db:
        db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
            policy_version,payload,result,attempts,stage) VALUES('owned-plan','youtube',?,'youtube_replay_plan_v4','video',
            'completed',?,?,'replay-fifo-v4',?,?,1,'youtube_whole_plan_obs')""",
            (source, now, now, encode(payload), encode(result)))
        db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
            policy_version,payload,attempts,error_code,stage) VALUES('owned-download','youtube',?,
            'youtube_replay_download_v4','video','awaiting_review',?,?,'replay-fifo-v4','{}',1,?,
            'youtube_whole_acquiring')""", (source, now, now, op.ERROR))
        db.execute("""UPDATE replay_fifo_v4 SET plan_job_id='owned-plan',download_job_id='owned-download',
            state='hold',reason=?""", (op.ERROR,))
    publish_revision(f)
    out, code = run(monkeypatch, capsys, '--apply')
    assert code == 0, out.get('errors')
    assert out['rebased'] and out['rebased'][0]['kind'] == 'replan'
    cand = f.db.query('SELECT * FROM replay_fifo_v4')[0]
    plan, download = f.db.get('owned-plan'), f.db.get('owned-download')
    assert (cand['state'], cand['download_job_id'], cand['reason']) == ('plan_queued', None, None)
    assert (plan['state'], plan['result']) == ('pending', {})
    assert plan['payload']['raw']['contentDetails']['duration'] == 'PT3591S'
    assert (download['state'], download['error_code']) == ('awaiting_review', op.ERROR)
    assert f.db.query('SELECT job_id FROM replay_fifo_retired_jobs_v4') == [{'job_id': 'owned-download'}]
    assert partial.read_bytes() == b'x' * 4096
    claimed = f.db.claim('video', 300, 4)
    assert claimed['job_id'] == 'owned-plan' and claimed['attempts'] == 2
    assert len(f.db.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))) == 1