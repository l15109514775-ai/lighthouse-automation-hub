"""Owned real SQLite/readiness fixtures for the single stale-snapshot rebase.

The plan job, readiness wait and failed observation child are produced by the real
V4 coordinator; only the extractor/transport seam is owned. The operator itself
runs against that genuine state and must preserve attempts, rows and audit.
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

from test_youtube_readiness_v4 import begin, readiness  # noqa: E402
from test_replay_fifo_v4 import fifo  # noqa: E402,F401  - fixture dependency of readiness

FILE = Path(__file__).resolve().parents[2] / 'scripts/ops/rebind_whole_replay_source_snapshot_v1.py'
SPEC = importlib.util.spec_from_file_location('owned_source_snapshot_rebase', FILE)
op = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(op)


def publish_revision(f, *, actual_end=None):
    """The post-processing official revision of the same finished live archive."""
    raw = deepcopy(f.raw)
    raw['contentDetails']['duration'] = 'PT3591S'                 # nine seconds shorter
    raw['contentDetails']['definition'] = 'hd'
    raw['snippet']['publishedAt'] = '2026-11-04T17:08:23Z'
    raw['status']['uploadStatus'] = 'processed'
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


@pytest.fixture
def held(readiness, monkeypatch):
    f = readiness
    # Owned resource seam: queue admission reads the pytest tmp filesystem and the
    # synthetic budget ledger; neither participates in the snapshot rebase.
    monkeypatch.setattr('adapters.ytdlp_whole_v4.WholeMaintenance.check_disk',
                        lambda self, reserve=0: 10 ** 15)
    monkeypatch.setattr('adapters.ytdlp_highest_v2.preflight_budget', lambda *a, **k: None)
    begin(f)
    with f.db.transaction() as db:
        db.execute('UPDATE youtube_readiness_waits_v4 SET next_check=?', (f.clock[0],))
    tick = f.coordinator.tick()
    created = tick['enqueued']
    assert len(created) == 1, tick
    child = created[0]
    raw = publish_revision(f)
    service = WholeReadinessObservationV4(f.s, f.db, coordinator=f.coordinator, downloader_factory=f.factory)
    assert Worker(f.s, f.db, {JOB_TYPE: service.process}).once('video')
    out = f.coordinator.tick()
    assert out['held'] and out['held'][0]['code'] == op.ERROR
    candidate = f.db.query('SELECT * FROM replay_fifo_v4')[0]
    wait = f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert (f.db.get(f.parent_id)['state'], f.db.get(f.parent_id)['attempts']) == ('upload_pending', 1)
    assert (candidate['state'], candidate['reason']) == ('hold', op.ERROR)
    assert (wait['state'], wait['active_child_id']) == ('hold', child)
    monkeypatch.setattr(op, 'SOURCE', raw['id'])
    monkeypatch.setattr(op, 'CHANNEL', raw['snippet']['channelId'])
    monkeypatch.setattr(op, 'CANDIDATE', candidate['candidate_id'])
    monkeypatch.setattr(op, 'JOB', f.parent_id)
    monkeypatch.setattr(op, 'WAIT_ID', wait['wait_id'])
    monkeypatch.setattr(op, 'CHILD', child)
    monkeypatch.setattr(op, 'ROOT', f.s.root.as_posix())
    monkeypatch.setattr(op, 'runtime_allowed', lambda: True)
    monkeypatch.setattr(op.Settings, 'load', staticmethod(lambda: f.s))
    return f, child


def run(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, 'argv', [str(FILE), '--maintenance-reference', 'owned targeted rebase', *args])
    op.main()
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_single_stale_snapshot_rebases_same_job_and_wait_then_normal_coordinator_resumes(held, monkeypatch, capsys):
    f, child = held
    before = f.db.get(f.parent_id)
    candidate_before = f.db.query('SELECT * FROM replay_fifo_v4')[0]
    plan = run(monkeypatch, capsys)
    assert plan['state'] == 'plan_only' and plan['attempts'] == 1 and plan['duration_delta_seconds'] == -9
    with pytest.raises(RuntimeError, match='REVIEWED_PLAN_CHANGED'):
        run(monkeypatch, capsys, '--apply', '--plan-sha256', '0' * 64)
    assert f.db.get(f.parent_id) == before
    result = run(monkeypatch, capsys, '--apply', '--plan-sha256', plan['plan_sha256'])
    job = f.db.get(f.parent_id)
    candidate = f.db.query('SELECT * FROM replay_fifo_v4')[0]
    wait = f.db.query('SELECT * FROM youtube_readiness_waits_v4')[0]
    assert result['state'] == 'stale_source_snapshot_rebased'
    assert (job['job_id'], job['attempts'], job['state'], job['stage'], job['error_code']) == \
        (before['job_id'], 1, 'retry_scheduled', 'waiting_readiness_v4', 'WAITING_YOUTUBE_READINESS')
    assert job['next_run'] > f.clock[0] + 3600 and job['lease_owner'] is None
    assert job['payload']['raw']['contentDetails']['duration'] == 'PT3591S'
    assert job['payload']['fifo_v4']['identity_sha256'] == candidate['identity_sha256']
    assert candidate['state'] == 'waiting_readiness' and candidate['reason'] is None
    assert candidate['identity_sha256'] != candidate_before['identity_sha256']
    assert candidate['registered_at'] == candidate_before['registered_at']
    assert (wait['state'], wait['active_child_id'], wait['wait_id']) == ('waiting_readiness', None, plan['wait_id'])
    record = json.loads(wait['record'])
    assert record['previous_hold']['error_code'] == op.ERROR and record['previous_hold']['child_job_id'] == child
    assert record['source_snapshot_rebase']['duration_delta_seconds'] == -9
    audit = f.db.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))
    assert len(audit) == 1 and json.loads(audit[0]['outcome'])['plan_sha256'] == plan['plan_sha256']
    assert not f.db.query('SELECT * FROM publications') and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')
    assert f.db.get(child)['state'] == 'awaiting_review' and f.db.get(child)['attempts'] == 1
    f.clock[0] = time.time() + 1                                  # the next ordinary producer tick
    resumed = f.coordinator.tick()['enqueued']
    assert len(resumed) == 1 and f.db.get(resumed[0])['source_id'] == f.raw['id'] + ':readiness:3'
    assert f.db.get(f.parent_id)['attempts'] == 1


def test_stable_identity_or_existing_publication_is_refused_without_any_write(held, monkeypatch, capsys):
    f, _ = held
    before = (f.db.query('SELECT * FROM jobs ORDER BY job_id'), f.db.query('SELECT * FROM replay_fifo_v4'),
              f.db.query('SELECT * FROM youtube_readiness_waits_v4'), f.db.query('SELECT * FROM audit'))
    publish_revision(f, actual_end='2026-11-01T02:02:00Z')
    with pytest.raises(RuntimeError, match='STABLE_IDENTITY_CHANGED'):
        run(monkeypatch, capsys)
    publish_revision(f)
    uid = ReplayFIFOV4(f.s, f.db).policy()['target_site_uid']
    with f.db.transaction() as db:
        db.execute("""INSERT INTO publications(idempotency_key,job_id,platform,source_platform,source_id,account_id,
            kind,state,updated_at) VALUES('owned-rebind-key',?,'bilibili','youtube',?,?,'owned','unknown',?)""",
            (f.parent_id, f.raw['id'], 'site_uid:' + uid, time.time()))
    with pytest.raises(RuntimeError, match='PUBLICATION_PRESENT'):
        run(monkeypatch, capsys)
    after = (f.db.query('SELECT * FROM jobs ORDER BY job_id'), f.db.query('SELECT * FROM replay_fifo_v4'),
             f.db.query('SELECT * FROM youtube_readiness_waits_v4'))
    assert after == before[:3]
    assert len(f.db.query('SELECT * FROM audit')) == len(before[3])
