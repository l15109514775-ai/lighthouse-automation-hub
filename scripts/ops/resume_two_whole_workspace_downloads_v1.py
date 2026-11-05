"""Restore only the two held real downloads after the qualified workspace fix.

No claim/fake lease, source edit, attempt reset, content-hash assertion, HTTP,
file deletion, new download job or publication. The ordinary worker revalidates.
"""
import argparse,hashlib,json,os,stat,time
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.locks import resource_guard
from services.hub.youtube_download_v4 import YouTubeWholeReplayDownload,STATE
from adapters.ytdlp_whole_v4 import needed_disk

VERSION='resume-two-workspace-failed-downloads-v1'
SOURCE_SHA='8e3fea2da8cbf13a64533eb62ffd5eb990c3f2f092ef47258edb8f63f6afd148'
TARGETS={
 'f920d1da-12dc-46e8-83d7-80b129ec1b68':('6wHqfDmNF0U','722c1f28-22d7-4683-8648-feafff1eeecb','ccc8a6d3-d89d-48de-800e-3308bf74b0db',208016614),
 '9801f01e-efb6-42ba-a233-c4124c3a7500':('YsSXSIYtCoE','38bd13fb-8737-4250-82da-c04504311a43','f922236d-4451-4e9a-afde-895fab15ebb7',280751510)}


def descriptor(file):
    assert file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    info=file.lstat();assert stat.S_ISREG(info.st_mode) and info.st_nlink==1
    return {'file':str(file),'bytes':info.st_size,'device':info.st_dev,'inode':info.st_ino,
        'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns,'mode':stat.S_IMODE(info.st_mode)}


def snapshot(settings,store,db):
    root=settings.root;assert root.as_posix()=='/opt/lighthouse-hub'
    assert hashlib.sha256((root/'adapters/ytdlp_whole_v4.py').read_bytes()).hexdigest()==SOURCE_SHA
    assert (root/'config/hub.toml').read_bytes()==(root/'config/video-runtime-v5.toml').read_bytes()
    service=YouTubeWholeReplayDownload(settings,store);policy=service.policy();items=[]
    for ident,(source,candidate,plan_job,partial_bytes) in TARGETS.items():
        job=dict(db.execute('SELECT * FROM jobs WHERE job_id=?',(ident,)).fetchone())
        item=dict(db.execute('SELECT * FROM replay_fifo_v4 WHERE candidate_id=?',(candidate,)).fetchone())
        assert (job['source_platform'],job['source_id'],job['job_type'],job['queue'],job['policy_version'],job['state'],job['error_code'],job['stage'],job['attempts'])==(
            'youtube',source,'youtube_replay_download_v4','video','replay-fifo-v4','awaiting_review','YOUTUBE_WHOLE_WORKSPACE_INVALID','youtube_whole_acquiring',1)
        assert job['lease_owner'] is job['lease_until'] is job['external_publish_id'] is None
        assert (item['source_id'],item['state'],item['reason'],item['plan_job_id'],item['download_job_id'],item['publication_job_id'])==(
            source,'hold','YOUTUBE_WHOLE_WORKSPACE_INVALID',plan_job,ident,None)
        assert not service.fifo._existing(db,source,except_jobs=(plan_job,ident))
        for table in ('youtube_track_custody_v4','replay_multipart_parts_v4','replay_delivery_outputs_v5'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                assert not db.execute('SELECT 1 FROM '+table+' WHERE job_id=?',(ident,)).fetchone()
        current=store.get(ident);binding=service.resource_binding(current)
        service.fifo._fresh(service.fifo.candidate(candidate),service.fifo.policy(),require_fresh=False)
        state=current['result'][STATE]
        assert state['binding']==binding and state['state']=='acquiring'
        assert state['human_review'] is state['publication_authorized'] is False
        assert not any(k in state for k in ('source_asset','receipt','parts','delivery_v5'))
        area=root/'media'/ident/'youtube-whole-v4';partial=descriptor(area/'stream-299.mp4.part')
        assert partial['bytes']==partial_bytes
        references={}
        for name in ('ticket.json','selection.json'):
            file=area/name;info=descriptor(file);assert 0<info['bytes']<262144
            references[name]={'file':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
        selection=json.loads((area/'selection.json').read_bytes())
        assert selection=={'binding_sha256':hashlib.sha256(encode(binding).encode()).hexdigest(),
            'source_plan':binding['source_plan'],'selected_formats':binding['source_plan']['selected_formats']}
        ticket=json.loads((area/'ticket.json').read_bytes());assert ticket['job_id']==ident and ticket['binding']==binding
        assert not (area/'original.mp4').exists() and not (area/'verified.json').exists()
        remaining=needed_disk(area,binding)
        items.append({'job':job,'candidate':item,'partial':partial,'small_references':references,'remaining_working_bytes':remaining})
    return {'version':VERSION,'items':items,'policy_sha256':policy['_sha256'],
        'runtime_config_sha256':hashlib.sha256((root/'config/hub.toml').read_bytes()).hexdigest(),
        'fixed_source_sha256':SOURCE_SHA,'attempts_reset':False,'source_changed':False,'publication_created':False}


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--plan-sha256');a=p.parse_args()
    assert os.getuid()==10001 and bool(a.apply)==bool(a.plan_sha256)
    settings=Settings.load();store=Store(settings.db_path)
    with resource_guard(settings.root/'data/locks/video'),resource_guard(settings.root/'data/locks/replay-fifo-dispatch-v4'):
        with store.transaction() as db:
            value=snapshot(settings,store,db);digest=hashlib.sha256(encode(value).encode()).hexdigest()
            if not a.apply:
                print(encode({'state':'plan_only','sha256':digest,'job_ids':list(TARGETS),'attempts_preserved':1,
                    'partial_bytes':{v['job']['job_id']:v['partial']['bytes'] for v in value['items']},'partial_content_hash_claimed':False}));return
            assert digest==a.plan_sha256;now=time.time()
            for item in value['items']:
                old=item['job'];candidate=item['candidate']
                assert descriptor(Path(item['partial']['file']))==item['partial']
                assert db.execute("UPDATE jobs SET state='retry_scheduled',error_code=NULL,next_run=?,updated_at=? WHERE job_id=? AND state='awaiting_review' AND updated_at=? AND attempts=1 AND lease_owner IS NULL AND lease_until IS NULL AND external_publish_id IS NULL",
                    (now,now,old['job_id'],old['updated_at'])).rowcount==1
                assert db.execute("UPDATE replay_fifo_v4 SET state='download_queued',reason=NULL,updated_at=? WHERE candidate_id=? AND state='hold' AND updated_at=? AND download_job_id=? AND publication_job_id IS NULL",
                    (now,candidate['candidate_id'],candidate['updated_at'],old['job_id'])).rowcount==1
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (now,'owner-authorized-maintenance',VERSION,old['job_id'],encode({'plan_sha256':digest,'previous_job':old,'previous_candidate':candidate,
                        'partial':item['partial'],'small_references':item['small_references'],'fixed_source_sha256':SOURCE_SHA,'attempts_reset':False,'partial_content_hash_claimed':False})))
    print(encode({'state':'resumed_existing_downloads','job_ids':list(TARGETS),'attempts_reset':False,'new_jobs_created':0,'publication_created':False}))


if __name__=='__main__':main()
