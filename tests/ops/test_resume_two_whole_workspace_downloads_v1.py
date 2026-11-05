"""Real SQLite/CAS/partial preservation; upstream policy admission is named seam."""
import hashlib,importlib.util,json,sys,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.hub.db import Store,encode

FILE=Path(__file__).parents[2]/'scripts/ops/resume_two_whole_workspace_downloads_v1.py'
spec=importlib.util.spec_from_file_location('owned_workspace_resume',FILE);op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)


@pytest.fixture
def owned(tmp_path,monkeypatch):
    class OwnedRootAdmission:
        def as_posix(self):return '/opt/lighthouse-hub'
        def __truediv__(self,value):return tmp_path/value
    adapter=Path(__file__).parents[2]/'adapters/ytdlp_whole_v4.py'
    target=tmp_path/'adapters/ytdlp_whole_v4.py';target.parent.mkdir();target.write_bytes(adapter.read_bytes())
    config=tmp_path/'config';config.mkdir()
    for n in ('hub.toml','video-runtime-v5.toml'):(config/n).write_bytes(b'owned=true\n')
    db=Store(tmp_path/'data/hub.sqlite3');db.init();now=time.time()
    monkeypatch.setattr(op,'TARGETS',{k:(*v[:3],5) for k,v in op.TARGETS.items()})
    with db.transaction() as conn:
        conn.execute('CREATE TABLE replay_fifo_v4(candidate_id TEXT PRIMARY KEY,source_id TEXT,state TEXT,reason TEXT,plan_job_id TEXT,download_job_id TEXT,publication_job_id TEXT,updated_at REAL)')
        for ident,(source,candidate,plan,_) in op.TARGETS.items():
            binding={'source_plan':{'required_working_bytes':1000000,'selected_formats':[{'format_id':'299','ext':'mp4','filesize':100}]}}
            state={'binding':binding,'state':'acquiring','human_review':False,'publication_authorized':False}
            conn.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,error_code,policy_version,payload,result,stage) VALUES(?,'youtube',?,'youtube_replay_download_v4','video','awaiting_review',?,?,1,'YOUTUBE_WHOLE_WORKSPACE_INVALID','replay-fifo-v4',?,?,'youtube_whole_acquiring')",
                (ident,source,now,now,encode({'owned_expected_binding':binding}),encode({op.STATE:state})))
            conn.execute("INSERT INTO replay_fifo_v4 VALUES(?,?,'hold','YOUTUBE_WHOLE_WORKSPACE_INVALID',?,?,NULL,?)",(candidate,source,plan,ident,now))
            area=tmp_path/'media'/ident/'youtube-whole-v4';area.mkdir(parents=True);(area/'stream-299.mp4.part').write_bytes(b'owned')
            (area/'selection.json').write_text(encode({'binding_sha256':hashlib.sha256(encode(binding).encode()).hexdigest(),'source_plan':binding['source_plan'],'selected_formats':binding['source_plan']['selected_formats']}))
            (area/'ticket.json').write_text(encode({'job_id':ident,'binding':binding}))
    class OwnedFIFOAdmission:
        def _existing(self,conn,source,**kw):
            return bool(conn.execute("SELECT 1 FROM publications WHERE source_id=?",(source,)).fetchone())
        def candidate(self,ident):return db.query('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(ident,))[0]
        def policy(self):return {}
        def _fresh(self,candidate,policy,*,require_fresh):assert require_fresh is False
    class OwnedSourcePolicyAdmission:
        def __init__(self,*args):self.fifo=OwnedFIFOAdmission()
        def policy(self):return {'_sha256':'a'*64}
        def resource_binding(self,current):return current['payload']['owned_expected_binding']
    settings=SimpleNamespace(root=OwnedRootAdmission(),db_path=db.path)
    monkeypatch.setattr(op,'YouTubeWholeReplayDownload',OwnedSourcePolicyAdmission)
    monkeypatch.setattr(op.Settings,'load',staticmethod(lambda:settings));monkeypatch.setattr(op.os,'getuid',lambda:10001,raising=False)
    return settings,db,tmp_path


def digest(settings,db):
    with db.transaction() as conn:return hashlib.sha256(encode(op.snapshot(settings,db,conn)).encode()).hexdigest()


def apply(monkeypatch,value):
    monkeypatch.setattr(sys,'argv',[str(FILE),'--apply','--plan-sha256',value]);op.main()


def test_resume_keeps_original_payload_result_partial_and_attempt_then_claims_two(owned,monkeypatch):
    settings,db,root=owned;before={i:db.get(i) for i in op.TARGETS};apply(monkeypatch,digest(settings,db))
    for ident in op.TARGETS:
        row=db.get(ident);assert row['state']=='retry_scheduled' and row['attempts']==1
        assert row['payload']==before[ident]['payload'] and row['result']==before[ident]['result'] and row['stage']==before[ident]['stage']
        assert (root/'media'/ident/'youtube-whole-v4/stream-299.mp4.part').read_bytes()==b'owned'
    assert db.claim('video',300,4)['attempts']==2
    assert len(db.query('SELECT * FROM audit'))==2 and not db.query('SELECT * FROM publications')


@pytest.mark.parametrize('change',['lease','publication','binding','partial','wrong_plan'])
def test_changed_scope_or_snapshot_refuses_without_queue_mutation(owned,monkeypatch,change):
    settings,db,root=owned;reviewed=digest(settings,db);ident=next(iter(op.TARGETS));now=time.time()
    if change=='lease':
        with db.transaction() as conn:conn.execute('UPDATE jobs SET lease_until=? WHERE job_id=?',(now+300,ident))
    elif change=='publication':
        with db.transaction() as conn:conn.execute("INSERT INTO publications VALUES('owned-key',?,'bilibili','youtube',?,'site_uid:owned','owned','unknown',NULL,NULL,?)",(ident,op.TARGETS[ident][0],now))
    elif change=='binding':
        row=db.get(ident);row['result'][op.STATE]['binding']={'changed':True}
        with db.transaction() as conn:conn.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(row['result']),ident))
    elif change=='partial':(root/'media'/ident/'youtube-whole-v4/stream-299.mp4.part').write_bytes(b'changed')
    else:reviewed='0'*64
    before=db.query('SELECT * FROM jobs ORDER BY job_id');fifo=db.query('SELECT * FROM replay_fifo_v4 ORDER BY candidate_id')
    with pytest.raises(AssertionError):apply(monkeypatch,reviewed)
    assert db.query('SELECT * FROM jobs ORDER BY job_id')==before and db.query('SELECT * FROM replay_fifo_v4 ORDER BY candidate_id')==fifo
    assert not db.query('SELECT * FROM audit')
