"""Resume only the two real, untouched inventory failures after the verified fix."""
import argparse,hashlib,json,os,time
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.locks import resource_guard
from services.hub.youtube_download_v4 import YouTubeWholeReplayPrepare

TARGETS={'ccc8a6d3-d89d-48de-800e-3308bf74b0db':('6wHqfDmNF0U','722c1f28-22d7-4683-8648-feafff1eeecb'),
 'f922236d-4451-4e9a-afde-895fab15ebb7':('YsSXSIYtCoE','38bd13fb-8737-4250-82da-c04504311a43')}
VERSION='resume-two-inventory-failed-plans-v1'
SOURCE_SHA='b6fb2c72a0ef1634a46bb31ef3784a751b0655c42a91279c7edca9f9dcc164a5'

def snapshot(settings,store,db):
    root=settings.root
    assert root.as_posix()=='/opt/lighthouse-hub'
    assert hashlib.sha256((root/'services/hub/youtube_natural_plan_v3.py').read_bytes()).hexdigest()==SOURCE_SHA
    assert (root/'config/hub.toml').read_bytes()==(root/'config/video-runtime-v5.toml').read_bytes()
    assert settings.config['youtube_whole_replay_download_enabled'] is True
    service=YouTubeWholeReplayPrepare(settings,store);policy=service.policy();items=[]
    for ident,(source,candidate) in TARGETS.items():
        job=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(ident,)).fetchone())
        item=dict(db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(candidate,)).fetchone())
        assert (job['source_platform'],job['source_id'],job['job_type'],job['queue'],job['state'],job['error_code'],job['stage'],job['attempts'])==('youtube',source,'youtube_replay_plan_v4','video','awaiting_review','YOUTUBE_NATURAL_SOURCE_PLAN_INVALID','discovered',2)
        assert job['lease_owner'] is job['lease_until'] is job['external_publish_id'] is None and json.loads(job['result'])=={}
        assert (item['source_id'],item['state'],item['reason'],item['plan_job_id'])==(source,'hold','YOUTUBE_NATURAL_SOURCE_PLAN_INVALID',ident)
        assert item['download_job_id'] is item['publication_job_id'] is None
        assert not db.execute("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(source,)).fetchone()
        service.resource_binding(store.get(ident));items.append({'job':job,'candidate':item})
    return {'version':VERSION,'items':items,'policy_sha256':policy['_sha256'],
      'runtime_config_sha256':hashlib.sha256((root/'config/hub.toml').read_bytes()).hexdigest(),
      'fixed_source_sha256':SOURCE_SHA,'attempts_reset':False,'source_changed':False,'publication_created':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--plan-sha256');a=p.parse_args()
    assert os.getuid()==10001 and bool(a.apply)==bool(a.plan_sha256)
    s=Settings.load();store=Store(s.db_path)
    with resource_guard(s.root/'data/locks/video'),resource_guard(s.root/'data/locks/replay-fifo-dispatch-v4'):
        with store.transaction() as db:
            value=snapshot(s,store,db);digest=hashlib.sha256(encode(value).encode()).hexdigest()
            if not a.apply:
                print(encode({'state':'plan_only','sha256':digest,'jobs':list(TARGETS),'attempts_preserved':2,'fixed_source_sha256':SOURCE_SHA}));return
            assert digest==a.plan_sha256;now=time.time()
            for item in value['items']:
                old=item['job'];candidate=item['candidate']
                assert db.execute("UPDATE jobs SET state='retry_scheduled',error_code=NULL,next_run=?,updated_at=? WHERE job_id=? AND state='awaiting_review' AND updated_at=? AND attempts=2 AND lease_owner IS NULL",(now,now,old['job_id'],old['updated_at'])).rowcount==1
                assert db.execute("UPDATE replay_fifo_v4 SET state='plan_queued',reason=NULL,updated_at=? WHERE candidate_id=? AND state='hold' AND updated_at=?",(now,candidate['candidate_id'],candidate['updated_at'])).rowcount==1
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,'owner-authorized-maintenance',VERSION,old['job_id'],encode({'plan_sha256':digest,'previous_job':old,'previous_candidate':candidate,'fixed_source_sha256':SOURCE_SHA,'attempts_reset':False})))
    print(encode({'state':'resumed_existing_plans','job_ids':list(TARGETS),'attempts_reset':False,'publication_created':False}))

if __name__=='__main__':main()
