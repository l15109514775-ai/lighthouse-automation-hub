"""Plan/apply a video-only, registration-only image change; preserve four peers."""
from pathlib import Path
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import select
import subprocess
import time
import uuid

ROOT=Path('/opt/lighthouse-hub')
EXPECTED_COMPOSE='792e99a7b31cbe669937fcc9a3307baf12de4b319ee748016c58cbf8889fbfe6'
ARCHIVE='c629e57814d57e4a701cc6845109ae974e34f9a9ac05a65cacdf766b32536472'
COMMIT='df104738bcb47dfe965742c785c102edf6337397'
PEERS=('api','scheduler','tweet-worker','system-worker')
COMMAND=['python','-m','services.hub.replay_multipart_worker_runtime_v4','--register-only']

def sha(body):return hashlib.sha256(body).hexdigest()
def run(args,**kw):return subprocess.run(args,capture_output=True,text=True,check=True,**kw)
def safe(p):
    assert p.is_absolute() and p.resolve()==p and not any(x.is_symlink() for x in (p,*p.parents))
    return p
def write(file,body,mode=0o640):
    temp=file.with_name(file.name+'.new')
    with temp.open('xb') as f:f.write(body);f.flush();os.fsync(f.fileno())
    os.chmod(temp,mode);os.chown(temp,0,10001);os.replace(temp,file)
    fd=os.open(file.parent,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
def inspect(service):
    cid=run(['docker','compose','-f',str(ROOT/'compose.yaml'),'ps','-q',service]).stdout.strip()
    assert len(cid)==64
    return json.loads(run(['docker','inspect',cid]).stdout)[0]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qualification',required=True)
    p.add_argument('--policy',required=True)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args();assert os.geteuid()==0
    qfile=safe(Path(a.qualification));q=json.loads(qfile.read_bytes())
    assert qfile.parent.parent==Path('/home/lighthouse-ops/lighthouse-evidence')
    assert q['version']=='tracked-source-candidate-ubuntu-result-v1' and q['archive_sha256']==ARCHIVE and q['source_commit']==COMMIT
    assert q['passed'] is True and q['tests']==268 and q['exit_code']==0
    assert q['network']=='none' and q['production_data_mounted'] is q['secrets_injected'] is q['oom_killed'] is q['timed_out'] is False
    assert q['failure']==q['error']==q['skipped']==0
    assert run(['docker','image','inspect','-f','{{.Id}}',q['image']]).stdout.strip()==q['image']
    policy_file=safe(Path(a.policy));policy=json.loads(policy_file.read_bytes())
    assert policy['version']=='replay-fifo-policy-v4' and policy['enabled'] is True
    assert policy['target_site_uid']=='1000000000000000' and policy['source_not_before']=='2026-10-31T16:00:00Z'
    assert policy['owner_queue_reference']=='Owner: 修改队列，并继续支持更长或更大的录播 (2026-11-02)'
    current=safe(ROOT/'compose.yaml');raw=current.read_bytes();assert sha(raw)==EXPECTED_COMPOSE
    before=json.loads(raw);after=deepcopy(before);svc=after['services']['video-worker']
    assert svc['image']=='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
    assert svc['command']==['python','-m','services.hub.replay_highest_moderation_v2b']
    removed=[];keep=[]
    for mount in svc['volumes']:
        if any(mount['target'].startswith(str(ROOT)+'/'+n+'/') for n in ('services','adapters','scheduler','vendor','scripts')):
            assert mount['type']=='bind' and mount['read_only'] is True and mount['source'].startswith('/usr/local/lib/')
            removed.append(mount)
        else:keep.append(mount)
    assert len(removed)==45 and len(keep)==8
    svc['volumes']=keep;svc['image']=q['image'];svc['command']=COMMAND
    reverse=deepcopy(after);reverse['services']['video-worker']=deepcopy(before['services']['video-worker']);assert reverse==before
    peer_before={n:inspect(n) for n in PEERS};old=inspect('video-worker')
    result={'version':'replay-v4-registration-deployment-v1','mode':'register_only','image':q['image'],'source_commit':COMMIT,
        'qualification':str(qfile),'qualification_sha256':sha(qfile.read_bytes()),'tests':q['tests'],
        'compose_before_sha256':sha(raw),'removed_legacy_source_file_mounts':45,'persistent_mounts_preserved':8,
        'download_enabled':False,'publication_enabled':False,'policy_sha256':sha(policy_file.read_bytes()),'applied':False}
    if not a.apply:print(json.dumps(result));return
    target=safe(ROOT/'config/replay-fifo-policy-v4.json')
    assert not target.exists() or target.read_bytes()==policy_file.read_bytes()
    folder=ROOT/'backups'/('v4-registration-deploy-'+str(uuid.uuid4()));folder.mkdir(mode=0o700)
    (folder/'compose.before.json').write_bytes(raw);(folder/'compose.before.json').chmod(0o600)
    dbcheck="""import json,sqlite3,sys;from pathlib import Path
from services.hub.locks import resource_guard
p=Path('/opt/lighthouse-hub/data/hub.sqlite3');c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
with resource_guard(p.parent/'locks/video',p.parent/'locks/moderation'):
 assert not c.execute(\"SELECT 1 FROM jobs WHERE queue IN ('video','moderation') AND state='processing' LIMIT 1\").fetchone()
 print(json.dumps({'video_processing':0,'video_and_moderation_locks_held':True}),flush=True)
 sys.stdin.buffer.read(1)
"""
    holder=None
    try:
        holder=subprocess.Popen(['docker','exec','--user','10001:10001','-i',old['Id'],'python','-c',dbcheck],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        assert select.select([holder.stdout],[],[],20)[0],'video lock holder did not become ready'
        locked=json.loads(holder.stdout.readline());assert locked['video_and_moderation_locks_held'] is True
        # Both consumers are idle and locked until their container stops. The
        # replacement is explicitly incapable of claiming jobs.
        run(['docker','compose','-f',str(current),'stop','--timeout','30','video-worker'],timeout=50)
        holder.stdin.close();holder.wait(timeout=15)
        if not target.exists():write(target,policy_file.read_bytes())
        encoded=(json.dumps(after,indent=2,ensure_ascii=False)+'\n').encode()
        write(current,encoded)
        run(['docker','compose','-f',str(current),'up','-d','--no-deps','--pull','never','video-worker'],timeout=120)
        new=inspect('video-worker');assert new['State']['Running'] and new['Image']==q['image'] and new['Config']['Cmd']==COMMAND
        for name,prior in peer_before.items():
            now=inspect(name);assert now['Id']==prior['Id'] and now['State']['StartedAt']==prior['State']['StartedAt']
        deadline=time.time()+35
        probe="""import json,sqlite3,time
c=sqlite3.connect('file:/opt/lighthouse-hub/data/hub.sqlite3?mode=ro',uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
b=c.execute(\"SELECT at FROM heartbeats WHERE name='worker:video-producer-v4'\").fetchone();assert b and 0<=time.time()-b['at']<90
r=[dict(x) for x in c.execute('SELECT candidate_id,source_id,state,actual_end,registered_at,plan_job_id,download_job_id,publication_job_id FROM replay_fifo_v4 WHERE actual_end IS NOT NULL ORDER BY actual_end,source_id')]
assert all(x['plan_job_id'] is None and x['download_job_id'] is None and x['publication_job_id'] is None for x in r)
assert any(x['source_id']=='6wHqfDmNF0U' and x['state']=='registered' for x in r) and any(x['source_id']=='YsSXSIYtCoE' and x['state']=='registered' for x in r)
print(json.dumps({'producer_heartbeat':b['at'],'candidates':r,'new_media_jobs':0,'new_publications':0}))"""
        while True:
            try:observed=json.loads(run(['docker','exec','--user','10001:10001',new['Id'],'python','-c',probe],timeout=15).stdout);break
            except subprocess.CalledProcessError:
                if time.time()>deadline:raise
                time.sleep(1)
        result.update(applied=True,new_video_container_id=new['Id'],old_video_container_id=old['Id'],
            compose_after_sha256=sha(current.read_bytes()),peer_containers_unchanged=True,observation=observed,rollback_compose=str(folder/'compose.before.json'))
    except BaseException as exc:
        if holder and holder.poll() is None:
            holder.stdin.close()
            try:holder.wait(timeout=15)
            except subprocess.TimeoutExpired:holder.terminate()
        failure={'version':result['version'],'state':'rollback_required','error_type':type(exc).__name__,
            'new_policy_preserved':target.exists(),'registered_candidates_preserved':True,'publications':0}
        try:
            if current.read_bytes()!=raw:write(current,raw)
            run(['docker','compose','-f',str(current),'up','-d','--no-deps','--pull','never','video-worker'],timeout=120)
            failure['state']='video_service_rolled_back'
        finally:write(folder/'failure-result.json',(json.dumps(failure,indent=2)+'\n').encode())
        raise
    output=folder/'deployment-result.json';write(output,(json.dumps(result,indent=2)+'\n').encode())
    print(json.dumps({'report':str(output),'applied':True,'mode':result['mode'],
        'new_video_container_id':result['new_video_container_id'],'peer_containers_unchanged':True,
        'download_enabled':False,'publication_enabled':False}))

if __name__=='__main__':main()
