"""Owned real SQLite/CAS recovery; no actual /opt source or policy claim.

The upstream root-path/policy/resource-binding admission is a named owned
fixture seam. Fixed target IDs, rows, leases, transaction, attempts and audit
use the actual operator. No downloader, HTTP, publication or real DB runs.
"""
from pathlib import Path
from types import SimpleNamespace
import hashlib,importlib.util,json,sys,time
import pytest

from services.hub.db import Store,encode

FILE=Path(__file__).resolve().parents[2]/'scripts/ops/resume_two_readiness_serialization_plans_v1.py'
SPEC=importlib.util.spec_from_file_location('owned_readiness_resume',FILE)
op=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(op)


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    class OwnedRootAdmission:
        def as_posix(self):return '/opt/lighthouse-hub'  # Explicit path-admission seam only.
        def __truediv__(self,name):return tmp_path/name
    source=Path(__file__).parents[2]/'services/hub/youtube_natural_plan_v3.py'
    area=tmp_path/'services/hub';area.mkdir(parents=True);(area/'youtube_natural_plan_v3.py').write_bytes(source.read_bytes())
    assert hashlib.sha256(source.read_bytes()).hexdigest()==op.SOURCE_SHA
    expected={}
    for name in op.FILES:
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);raw=('owned fixed runtime '+name).encode();target.write_bytes(raw);expected[name]=hashlib.sha256(raw).hexdigest()
    config=tmp_path/'config';config.mkdir()
    for name in ('hub.toml','video-runtime-v5.toml'):(config/name).write_bytes(b'owned_fixture=true\n')
    db=Store(tmp_path/'data/hub.sqlite3');db.init();now=time.time()
    with db.transaction() as conn:
        conn.execute('CREATE TABLE replay_fifo_v4(candidate_id TEXT PRIMARY KEY,source_id TEXT,state TEXT,reason TEXT,plan_job_id TEXT,download_job_id TEXT,publication_job_id TEXT,updated_at REAL)')
        for ident,(source_id,candidate) in op.TARGETS.items():
            conn.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,error_code,policy_version,payload,result,stage) VALUES(?,'youtube',?,'youtube_replay_plan_v4','video','failed',?,?,1,'YOUTUBE_READINESS_RAW_NOT_JSON','replay-fifo-v4',?,'{}','discovered')",
                (ident,source_id,now,now,encode({'owned_original_payload':source_id})))
            conn.execute("INSERT INTO replay_fifo_v4 VALUES(?,?,'hold','YOUTUBE_READINESS_RAW_NOT_JSON',?,NULL,NULL,?)",(candidate,source_id,ident,now))
    with db.transaction() as conn:conn.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
    settings=SimpleNamespace(runtime_pins=expected,root=OwnedRootAdmission(),db_path=db.path,config={'youtube_whole_replay_download_enabled':True})
    class OwnedPolicyBoundary:
        def __init__(self,s,store):assert s is settings;self.store=store
        def policy(self):return {'_sha256':'a'*64}
        def resource_binding(self,job):assert job['payload']=={'owned_original_payload':op.TARGETS[job['job_id']][0]}
    monkeypatch.setattr(op,'YouTubeWholeReplayPrepare',OwnedPolicyBoundary)
    monkeypatch.setattr(op.Settings,'load',staticmethod(lambda:settings));monkeypatch.setattr(op.os,'getuid',lambda:10001,raising=False)
    return settings,db


def plan(settings,db):
    with db.transaction() as conn:value=op.snapshot(settings,db,conn,settings.runtime_pins)
    return hashlib.sha256(encode(value).encode()).hexdigest()


def run(monkeypatch,digest):
    monkeypatch.setattr(sys,'argv',[str(FILE),'--runtime-pins',json.dumps(op.Settings.load().runtime_pins),'--apply','--plan-sha256',digest]);op.main()


def test_exact_recovery_keeps_attempt_one_payload_and_next_real_claim_is_two(fixture,monkeypatch):
    settings,db=fixture;before={i:db.get(i) for i in op.TARGETS};run(monkeypatch,plan(settings,db))
    for ident in op.TARGETS:
        row=db.get(ident)
        assert row['state']=='retry_scheduled' and row['attempts']==1 and row['error_code'] is None
        assert row['payload']==before[ident]['payload'] and row['result']=={} and row['stage']=='discovered'
        assert row['lease_owner'] is row['lease_until'] is row['external_publish_id'] is None
    assert len(db.query("SELECT * FROM audit WHERE action=?",(op.VERSION,)))==2
    assert all(r['state']=='plan_queued' for r in db.query('SELECT * FROM replay_fifo_v4'))
    with db.transaction() as conn:conn.execute("UPDATE pipelines SET paused=0 WHERE name='video'")
    claimed=db.claim('video',300,4);assert claimed['attempts']==2
    assert not db.query('SELECT * FROM publications') and not db.query('SELECT * FROM api_calls')


def test_active_lease_is_refused_without_stealing_or_reset(fixture,monkeypatch):
    settings,db=fixture;ident=next(iter(op.TARGETS))
    with db.transaction() as conn:conn.execute("UPDATE jobs SET state='processing',lease_owner='owned-real-active-lease',lease_until=? WHERE job_id=?",(time.time()+300,ident))
    before=db.query('SELECT * FROM jobs ORDER BY job_id')
    with pytest.raises(AssertionError):run(monkeypatch,'0'*64)
    assert db.query('SELECT * FROM jobs ORDER BY job_id')==before and not db.query('SELECT * FROM audit')


def test_existing_publication_even_unknown_blocks_both_resumes(fixture,monkeypatch):
    settings,db=fixture;ident=next(iter(op.TARGETS));source=op.TARGETS[ident][0]
    with db.transaction() as conn:
        conn.execute("INSERT INTO publications(idempotency_key,job_id,platform,source_platform,source_id,account_id,kind,state,updated_at) VALUES('owned-key',?,'bilibili','youtube',?,'site_uid:1000000000000000','owned','unknown',?)",(ident,source,time.time()))
    before=db.query('SELECT * FROM jobs ORDER BY job_id')
    with pytest.raises(AssertionError):run(monkeypatch,'0'*64)
    assert db.query('SELECT * FROM jobs ORDER BY job_id')==before
    assert len(db.query('SELECT * FROM publications'))==1 and not db.query('SELECT * FROM audit')


@pytest.mark.parametrize('attempt',[0,2,3])
def test_nonmatching_attempt_history_is_never_reset(fixture,monkeypatch,attempt):
    _,db=fixture
    with db.transaction() as conn:conn.execute('UPDATE jobs SET attempts=?',(attempt,))
    before=db.query('SELECT * FROM jobs ORDER BY job_id')
    with pytest.raises(AssertionError):run(monkeypatch,'0'*64)
    assert db.query('SELECT * FROM jobs ORDER BY job_id')==before and not db.query('SELECT * FROM audit')


def test_wrong_reviewed_snapshot_hash_rolls_back_without_any_queue_mutation(fixture,monkeypatch):
    _,db=fixture;before=db.query('SELECT * FROM jobs ORDER BY job_id');fifo=db.query('SELECT * FROM replay_fifo_v4 ORDER BY candidate_id')
    with pytest.raises(AssertionError):run(monkeypatch,'f'*64)
    assert db.query('SELECT * FROM jobs ORDER BY job_id')==before
    assert db.query('SELECT * FROM replay_fifo_v4 ORDER BY candidate_id')==fifo and not db.query('SELECT * FROM audit')
