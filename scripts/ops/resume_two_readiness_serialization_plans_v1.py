"""Resume only the two exact failed metadata plans after the qualified readiness serialization fix after the verified fix."""
import argparse,hashlib,json,os,re,time
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.locks import resource_guard
from services.hub.youtube_download_v4 import YouTubeWholeReplayPrepare

TARGETS={'b85a9a38-8591-4272-b25a-11b868f708ec':('synthC00003','9064b1bf-74dd-4094-9460-e699ec6cd7cc'),
 '63393c71-a0da-483c-9a79-d98c012e0bbd':('EoZFofw4OSU','1c64e81b-c3ec-4218-b7c0-e0235ebc918c')}
VERSION='resume-two-readiness-serialization-plans-v1'
SOURCE_SHA='e6763f9a8ec4a4a2fe95ccd061a36d4701499a9f32cc0797cace4b8ce2467ee9'
FILES=('adapters/youtube_readiness_evidence_v3.py','adapters/ytdlp_whole_v4.py','services/hub/youtube_readiness_v4.py')

def snapshot(settings,store,db,expected):
    root=settings.root
    assert root.as_posix()=='/opt/lighthouse-hub'
    assert set(expected)==set(FILES) and all(re.fullmatch('[a-f0-9]{64}',v) for v in expected.values())
    for name,pin in expected.items():
        file=root/name
        assert file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)) and hashlib.sha256(file.read_bytes()).hexdigest()==pin
    assert db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']==1
    assert not db.execute("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)").fetchone()
    assert hashlib.sha256((root/'services/hub/youtube_natural_plan_v3.py').read_bytes()).hexdigest()==SOURCE_SHA
    assert (root/'config/hub.toml').read_bytes()==(root/'config/video-runtime-v5.toml').read_bytes()
    assert settings.config['youtube_whole_replay_download_enabled'] is True
    service=YouTubeWholeReplayPrepare(settings,store);policy=service.policy();items=[]
    for ident,(source,candidate) in TARGETS.items():
        job=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(ident,)).fetchone())
        item=dict(db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(candidate,)).fetchone())
        assert (job['source_platform'],job['source_id'],job['job_type'],job['queue'],job['state'],job['error_code'],job['stage'],job['attempts'])==('youtube',source,'youtube_replay_plan_v4','video','failed','YOUTUBE_READINESS_RAW_NOT_JSON','discovered',1)
        assert job['lease_owner'] is job['lease_until'] is job['external_publish_id'] is None and json.loads(job['result'])=={}
        assert (item['source_id'],item['state'],item['reason'],item['plan_job_id'])==(source,'hold','YOUTUBE_READINESS_RAW_NOT_JSON',ident)
        assert item['download_job_id'] is item['publication_job_id'] is None
        assert not db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(source,)).fetchone()
        service.resource_binding(store.get(ident));items.append({'job':job,'candidate':item})
    return {'version':VERSION,'items':items,'policy_sha256':policy['_sha256'],
      'runtime_config_sha256':hashlib.sha256((root/'config/hub.toml').read_bytes()).hexdigest(),
      'fixed_source_sha256':SOURCE_SHA,'readiness_runtime_pins':expected,'attempts_reset':False,'source_changed':False,'publication_created':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime-pins',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--plan-sha256');a=p.parse_args()
    assert os.getuid()==10001 and bool(a.apply)==bool(a.plan_sha256)
    s=Settings.load();store=Store(s.db_path)
    with resource_guard(s.root/'data/locks/video'),resource_guard(s.root/'data/locks/replay-fifo-dispatch-v4'):
        with store.transaction() as db:
            value=snapshot(s,store,db,json.loads(a.runtime_pins));digest=hashlib.sha256(encode(value).encode()).hexdigest()
            if not a.apply:
                print(encode({'state':'plan_only','sha256':digest,'jobs':list(TARGETS),'attempts_preserved':1,'fixed_source_sha256':SOURCE_SHA}));return
            assert digest==a.plan_sha256;now=time.time()
            for item in value['items']:
                old=item['job'];candidate=item['candidate']
                assert db.execute("UPDATE jobs SET state='retry_scheduled',error_code=NULL,next_run=?,updated_at=? WHERE job_id=? AND state='failed' AND updated_at=? AND attempts=1 AND lease_owner IS NULL AND lease_until IS NULL AND external_publish_id IS NULL",(now,now,old['job_id'],old['updated_at'])).rowcount==1
                assert db.execute("UPDATE replay_fifo_v4 SET state='plan_queued',reason=NULL,updated_at=? WHERE candidate_id=? AND state='hold' AND updated_at=?",(now,candidate['candidate_id'],candidate['updated_at'])).rowcount==1
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,'owner-authorized-maintenance',VERSION,old['job_id'],encode({'plan_sha256':digest,'previous_job':old,'previous_candidate':candidate,'fixed_source_sha256':SOURCE_SHA,'readiness_runtime_pins':value['readiness_runtime_pins'],'attempts_reset':False})))
    print(encode({'state':'resumed_existing_plans','job_ids':list(TARGETS),'attempts_reset':False,'publication_created':False}))

if __name__=='__main__':main()
