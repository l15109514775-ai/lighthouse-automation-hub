"""Video-only contention fix. Default stages and verifies an offline plan.

Apply preserves the existing video pause; only the image, retention proof
mount and matching release change. No job retry, claim or owner-window action.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import argparse,hashlib,json,os,re,sqlite3,stat,time,types,uuid
try:import fcntl
except ImportError:fcntl=None

ROOT=Path('/opt/lighthouse-hub');STATE=Path('/var/lib/lighthouse-replay-contention42-v1')
CURRENT='aec31c4f610e4c31c0d82db70ea17c5fca7bed338abe1daf7babfed8bcf81fcb'
OLD_IMAGE='sha256:06ed6b8e6adff8fd2a43e5345c8d1d1d1f62c463fd79d362d2df4dfdac1705e4'
IMAGE='sha256:24b0198ba425f527655173c2ef1432445947ebec5fed8660c6bb5527eaa15dde'
DEPLOY_SHA='e0e24d11d322f419db5a6092e1176b12d938b5577919c67977d28fe5d60813aa'
HELPER_SHA='4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
QUALIFIED=Path('/var/lib/lighthouse-backup-ops/replay-moderation-contention42-v5-tests/3db974c7-6013-4fed-b433-3063b3cb95fc/test-result.json')
QUALIFIED_SHA='e3c646987e7660a232b9e6cddb5e6a449795bb6b05e0204ced50235983684a5f'
PROOF=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/529d0c40-7ec6-46ca-b31c-c76ed6a34db4/test-attestation.json')
PROOF_SHA='ab8158d642c05a401f63c482c1994b458d82b8e35451016f015ff779e909b855'
OLD_PROOF=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/714563fb-9ed8-4316-aec8-f6a53b4ab64a')
OLD_PROOF_SHA='dc879928b0a741b673d3e637d3fecf76146804d6bac2f1747a957407c2dc5244'
OWNER=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/owner-authorization-1fb26fc9.json')
OWNER_SHA='8869d3ad9f68c317030ebbfdf5211075a624563e0adf9f653317a03747910eb9'
SOURCE='services/hub/replay_parts_worker_runtime_v5.py'
REQUIRED_BEATS={'worker:video-main-v5','worker:video-producer-v5','worker:video-part-moderation-v5'}

def need(value,code):
    if not value:raise ValueError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def env_map(values):
    pairs=[v.split('=',1) for v in values];need(all(len(v)==2 for v in pairs),'INVALID_ENV')
    result=dict(pairs);need(len(result)==len(pairs),'DUPLICATE_ENV');return result
def load(path,checksum,name):
    raw=Path(path).read_bytes();need(sha(raw)==checksum,'FROZEN_HELPER_CHANGED')
    module=types.ModuleType(name);module.__file__=str(path);exec(compile(raw,str(path),'exec'),module.__dict__);return module

def candidate(before,proof_folder):
    after=deepcopy(before);service=after['services']['video-worker']
    need(service['image']==OLD_IMAGE and proof_folder==PROOF.parent,'EXACT_IMAGE_AND_PROOF_REQUIRED')
    service['image']=IMAGE;mounts=[v for v in service['volumes'] if v['target']=='/run/qualifications/retention-v5']
    need(len(mounts)==1 and mounts[0]['source']==str(OLD_PROOF) and mounts[0]['read_only'] is True and
         mounts[0]['type']=='bind' and mounts[0].get('bind',{}).get('create_host_path') is False,'CURRENT_PROOF_MOUNT_CHANGED')
    mounts[0]['source']=str(proof_folder)
    reverse=deepcopy(after);reverse['services']['video-worker']['image']=OLD_IMAGE
    next(v for v in reverse['services']['video-worker']['volumes'] if v['target']=='/run/qualifications/retention-v5')['source']=str(OLD_PROOF)
    need(reverse==before,'ONLY_IMAGE_AND_PROOF_MAY_CHANGE');return after

def fingerprint(info):
    return {'id':info['Id'],'image':info['Image'],'pid':info['State']['Pid'],'started_at':info['State']['StartedAt'],'running':info['State']['Running'],
        'env_sha256':sha(wire(env_map(info['Config']['Env']))),'host_config_sha256':sha(wire(info['HostConfig'])),
        'mounts_sha256':sha(wire(info['Mounts'])),'process_sha256':sha(wire({k:info['Config'][k] for k in ('Cmd','User','Entrypoint','WorkingDir','Hostname')}))}

def verify_runtime(old,new,service):
    need(new['Image']==IMAGE and new['State']['Running'] is True,'NEW_VIDEO_NOT_RUNNING')
    need(env_map(new['Config']['Env'])==env_map(old['Config']['Env']),'ENV_CHANGED')
    for key in ('Cmd','User','Entrypoint','WorkingDir'):need(new['Config'][key]==old['Config'][key],'PROCESS_CONFIG_CHANGED')
    host=service.get('hostname')
    need(old['Config']['Hostname']==(host or old['Id'][:12]) and new['Config']['Hostname']==(host or new['Id'][:12]),'HOSTNAME_CHANGED')
    before,after=deepcopy(old['HostConfig']),deepcopy(new['HostConfig'])
    for value in (before,after):value.pop('Mounts',None);value.pop('Binds',None)
    need(before==after,'HOST_LIMITS_CHANGED')
    mounts=deepcopy(new['Mounts']);proof=[m for m in mounts if m['Destination']=='/run/qualifications/retention-v5']
    need(len(proof)==1 and proof[0]['Type']=='bind' and proof[0]['Source']==str(PROOF.parent) and proof[0]['RW'] is False,'NEW_PROOF_MOUNT_CHANGED')
    proof[0]['Source']=str(OLD_PROOF);need(mounts==old['Mounts'],'OTHER_MOUNTS_CHANGED')

def paused_snapshot(file):
    with sqlite3.connect('file:'+str(file)+'?mode=ro&immutable=1',uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()
        need(row and row['paused']==1,'MAINTENANCE_PAUSE_REQUIRED')
        rows=[dict(v) for v in db.execute("SELECT * FROM jobs WHERE queue='video' ORDER BY job_id")]
        need(not any(v['state']=='processing' or v['lease_owner'] is not None or v['lease_until'] is not None for v in rows),'ACTIVE_VIDEO_REFUSED')
        return sha(wire(rows))

def validate_qualifications(m,draw,dxml,praw,pxml):
    need(sha(draw)==QUALIFIED_SHA and sha(praw)==PROOF_SHA,'EXACT_ACTUAL_QUALIFICATION_REQUIRED')
    delta=json.loads(draw);proof=json.loads(praw)
    groups={'tests.test_replay_part_moderation_contention_v5':10,'tests.test_replay_parts_runtime_v5':9,'tests.test_replay_part_moderation_v5':23}
    need(delta['version']=='replay-moderation-contention42-ubuntu-v5' and delta['image']==IMAGE and delta['base_image']==OLD_IMAGE and
        delta['passed'] is True and delta['tests']==42 and delta['test_groups']==groups and m.junit(dxml,42)==groups and
        sha(dxml)==delta['junit_sha256'],'ACTUAL_42_REQUIRED')
    need(delta['source_before']==delta['source_after']==delta['source_hashes'] and len(delta['source_hashes'])==853 and
        delta['source_layer_delta']==1 and delta['resource_limits_passed'] is True and delta['production_all_five_unchanged'] is True,'ACTUAL_42_RUNTIME_REQUIRED')
    need(proof['version']==m.VERSION and proof['image']==IMAGE and proof['tests']==26 and
        proof['qualified_parent_sha256']==QUALIFIED_SHA and proof['source_files']==m.source_files(delta['source_manifest']) and
        proof['source_manifest_sha256']==sha(wire(delta['source_manifest'])) and proof['qualification_lineage']['contention42_run_sha256']==QUALIFIED_SHA and
        proof['qualification_lineage']['prior_lineage']==delta['parent_qualification_lineage'],'ACTUAL_26_SOURCE_REQUIRED')
    need(sha(pxml)==proof['junit_sha256'] and m.junit(pxml,26,group=m.GROUP)==proof['test_groups'],'ACTUAL_26_JUNIT_REQUIRED')
    need(all(proof[k]==0 for k in ('exit_code','failures','errors','skipped')) and proof['network']=='none' and
        all(proof[k] is True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','mounted_sources_unchanged','production_all_five_unchanged')) and
        all(proof[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed','timed_out')),'ACTUAL_26_ISOLATION_REQUIRED')
    return delta,proof

APP=r'''import hashlib,json,os,sqlite3
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5
def process_idle(rows,pid):
 for ident,args in rows:
  if ident==pid or not args:continue
  name=Path(args[0]).name
  if ident==1 and name in ('docker-init','tini'):continue
  if name.startswith('python') and args[1:]==['-m','services.hub.replay_parts_worker_runtime_v5','--publish']:continue
  raise ValueError('VIDEO_CHILD_PROCESS_STILL_ACTIVE')
def probe(full):
 s=Settings.load();c=sqlite3.connect(s.db_path.as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
 assert c.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()==(1,)
 assert not c.execute("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)").fetchall()
 result={'paused':True,'active_video_jobs':0,'heartbeats':dict(c.execute("SELECT name,at FROM heartbeats WHERE name IN ('worker:video-main-v5','worker:video-producer-v5','worker:video-part-moderation-v5')"))};c.close()
 if full:
  rows=[]
  for p in Path('/proc').iterdir():
   if not p.name.isdigit():continue
   try:args=[v.decode('utf8','replace') for v in (p/'cmdline').read_bytes().split(b'\0') if v]
   except (FileNotFoundError,ProcessLookupError,PermissionError):continue
   rows.append((int(p.name),args))
  process_idle(rows,os.getpid())
  ReplayPartsRetentionV5(s,Store(s.db_path))._release()
  raw=(s.root/'config/replay-parts-retention-v5-release.json').read_bytes();release=json.loads(raw)
  assert all(hashlib.sha256((s.root/n).read_bytes()).hexdigest()==v for n,v in release['source_files'].items())
  result.update(release_sha256=hashlib.sha256(raw).hexdigest(),runtime_source_count=len(release['source_files']),no_live_child=True)
 print(json.dumps(result))
'''

def probe(d,cid,*,full=False):
    return json.loads(d.run(['docker','exec','--user','10001:10001',cid,'python','-c',APP+'\nprobe('+str(full)+')'],timeout=60).stdout)

def advancing(beats,started,first,now):
    if set(beats)!=REQUIRED_BEATS or not all(started<=v<=now+1 for v in beats.values()):return False
    return first is not None and all(beats[k]>first[k] for k in beats)

@contextmanager
def idle_locks(d):
    with d.idle_locks():
        handles=[]
        try:
            for name in ('bilibili-video-profile','heavy'):
                file=ROOT/'data/locks'/name/'observer.lock'
                need(file.resolve()==file and not any(v.is_symlink() for v in (file,*file.parents)),'UNSAFE_IDLE_LOCK')
                fd=os.open(file,os.O_RDWR|os.O_NOFOLLOW);handles.append(fd);value=os.fstat(fd);actual=file.stat()
                need(stat.S_ISREG(value.st_mode) and value.st_uid==10001 and value.st_nlink==1 and
                    (value.st_dev,value.st_ino)==(actual.st_dev,actual.st_ino),'IDLE_LOCK_CHANGED')
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:
            for fd in reversed(handles):os.close(fd)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--deploy-helper',required=True);p.add_argument('--retention-helper',required=True)
    p.add_argument('--apply',action='store_true');a=p.parse_args()
    need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_HOST_REQUIRED');os.umask(0o077)
    d=load(a.deploy_helper,DEPLOY_SHA,'frozen_pilot_deployer');m=load(a.retention_helper,HELPER_SHA,'frozen_retention276')
    draw=m.read(QUALIFIED,protected=True);praw=m.read(PROOF,protected=True)
    delta,proof=validate_qualifications(m,draw,m.read(Path(json.loads(draw)['junit_file']),protected=True),praw,m.read(PROOF.parent/'results.xml',protected=True))
    old_image=m.inspect_image(OLD_IMAGE);image=m.inspect_image(IMAGE)
    need(image['RootFS']['Layers'][:-1]==old_image['RootFS']['Layers'],'ONE_COPY_LAYER_REQUIRED')
    authorization=json.loads(m.read(OWNER,protected=True));need(m.owner(authorization)==OWNER_SHA,'ORIGINAL_OWNER_REQUIRED')
    release=m.release_template(authorization,proof,praw);release_raw=wire(release)+b'\n'
    old=d.inspect('video-worker');need(old['Id']==CURRENT and old['Image']==OLD_IMAGE and old['State']['Running'],'CURRENT_VIDEO_CHANGED')
    before_raw=d.read(ROOT/'compose.yaml');before=json.loads(before_raw);after=candidate(before,PROOF.parent)
    peers={name:fingerprint(d.inspect(name)) for name in d.PEERS}
    configs={name:d.read(ROOT/'config'/name) for name in (*d.FIXED_CONFIG,*d.NAMES)}
    prior=json.loads(configs[m.RELEASE]);need(prior['test_attestation']['sha256']==OLD_PROOF_SHA and prior['authorization_sha256']==OWNER_SHA and
        set(prior['source_files'])==set(release['source_files']) and
        {n for n in prior['source_files'] if prior['source_files'][n]!=release['source_files'][n]}=={SOURCE},'ONLY_RUNTIME_RELEASE_SOURCE_MAY_CHANGE')
    m.root_directory(STATE);folder=m.root_directory(STATE/str(uuid.uuid4()))
    report={'version':'replay-contention42-cutover-v1','state':'plan_only','phase':'snapshot','image':IMAGE,'old_cid':CURRENT,
        'qualification42_sha256':QUALIFIED_SHA,'retention26_sha256':PROOF_SHA,'other_four':peers,'pause_preserved':True,
        'job_retry_or_claim':False,'owner_window_touched':False,'full_ready_deferred_until_root_unpause':True}
    try:
        d.save(folder/'snapshot.sqlite3',d.host_snapshot());paused_snapshot(folder/'snapshot.sqlite3')
        d.save(folder/'compose.before.json',before_raw,0o600);d.save(folder/'release.before.json',configs[m.RELEASE])
        d.save(folder/'runtime-before.filtered.json',wire(fingerprint(old))+b'\n')
        cfg=m.root_directory(folder/'config');private={**configs,'hub.toml':configs['video-runtime-v5.toml'],m.RELEASE:release_raw}
        seccomp=m.read(ROOT/'config/browser-seccomp.json');need(sha(seccomp)==delta['source_manifest']['files']['config/browser-seccomp.json']['sha256'],'SECCOMP_CHANGED')
        private['browser-seccomp.json']=seccomp
        for name,value in private.items():d.save(cfg/name,value)
        d.save(folder/'browser-seccomp.json',seccomp);report['phase']='preflight'
        report['preflight']=d.preflight(IMAGE,folder,PROOF.parent,before['services']['video-worker']['environment'],seccomp=folder/'browser-seccomp.json')
        with idle_locks(d):
            need(fingerprint(d.inspect('video-worker'))==fingerprint(old) and d.read(ROOT/'compose.yaml')==before_raw,'CURRENT_VIDEO_CHANGED_AT_IDLE')
            need(all(fingerprint(d.inspect(n))==v for n,v in peers.items()),'OTHER_SERVICE_CHANGED')
            need(all(d.read(ROOT/'config'/n)==v for n,v in configs.items()),'CONFIG_CHANGED')
            report['idle_probe']=probe(d,CURRENT,full=True)
            need(report['idle_probe']['paused'] is True and report['idle_probe']['release_sha256']==sha(configs[m.RELEASE]),'PAUSED_IDLE_REQUIRED')
            if not a.apply:
                d.save(folder/'report.json',wire(report)+b'\n');print(json.dumps({'state':'verified_plan_only','report':str(folder/'report.json')}));return
            report['phase']='before_stop';d.save(folder/'before-stop.sqlite3',d.host_snapshot());rows=paused_snapshot(folder/'before-stop.sqlite3')
            d.save(folder/'report.json',wire(report)+b'\n');d.run(d.command('stop','--timeout','45','video-worker'),timeout=60)
            need(not json.loads(d.run(['docker','inspect',CURRENT]).stdout)[0]['State']['Running'],'OLD_VIDEO_STILL_RUNNING')
            report.update(state='installed_restart_pending',phase='install');d.save(folder/'report.json',wire(report)+b'\n')
            d.save(ROOT/'config'/m.RELEASE,release_raw);d.save(ROOT/'compose.yaml',wire(after)+b'\n')
            d.save(folder/'after-install.sqlite3',d.host_snapshot());need(paused_snapshot(folder/'after-install.sqlite3')==rows,'VIDEO_ROWS_CHANGED')
        # Release all queue/profile/heavy locks before starting live workers.
        report['phase']='start';d.run(d.command('up','-d','--no-deps','--no-build','--pull','never','video-worker'),timeout=120)
        new=d.inspect('video-worker');verify_runtime(old,new,after['services']['video-worker'])
        checked=probe(d,new['Id'],full=True);need(checked['release_sha256']==sha(release_raw) and checked['runtime_source_count']==141,'INSTALLED_SOURCE_RELEASE_REQUIRED')
        started=datetime.fromisoformat(new['State']['StartedAt'].replace('Z','+00:00')).timestamp();deadline=time.monotonic()+180;first=None;report['phase']='paused_heartbeats'
        while True:
            check=probe(d,new['Id']);beats=check['heartbeats']
            if advancing(beats,started,first,time.time()):break
            if set(beats)==REQUIRED_BEATS and all(started<=v<=time.time()+1 for v in beats.values()) and first is None:first=beats.copy()
            need(time.monotonic()<deadline,'PAUSED_HEARTBEATS_NOT_ADVANCING');time.sleep(2)
        need(all(fingerprint(d.inspect(n))==v for n,v in peers.items()),'OTHER_SERVICE_CHANGED_AFTER_START')
        need(all(d.read(ROOT/'config'/n)==v for n,v in configs.items() if n!=m.RELEASE),'OTHER_CONFIG_CHANGED_AFTER_START')
        need(d.read(ROOT/'compose.yaml')==wire(after)+b'\n' and d.read(ROOT/'config'/m.RELEASE)==release_raw,'INSTALLED_BYTES_CHANGED')
        report.update(state='video_contention_fix_active_paused',phase='completed',new_cid=new['Id'],heartbeats=beats,heartbeat_baseline=first,
            installed_runtime=checked,other_services_unchanged=True,pause_preserved=True)
        d.save(folder/'report.json',wire(report)+b'\n');print(json.dumps({'state':report['state'],'report':str(folder/'report.json'),'new_cid':new['Id']}))
    except BaseException as error:
        code=getattr(error,'code',None)
        if isinstance(error,ValueError) and len(error.args)==1:code=error.args[0]
        if not isinstance(code,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code):code=None
        report.update(state='requires_reconciliation',error_class=type(error).__name__,error_code=code)
        d.save(folder/'report.json',wire(report)+b'\n');raise

if __name__=='__main__':main()
