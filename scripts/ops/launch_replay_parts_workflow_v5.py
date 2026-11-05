"""Plan an acceptance-bound V5 formal launch; apply only while video is idle.

No image build, provider/browser call, queue retry, media deletion or new owner
approval. Plan prints JSON only. Apply consumes that saved, exact plan.
"""
from copy import deepcopy
from datetime import datetime,timezone
import argparse
from contextlib import contextmanager
import hashlib,json,os,re,stat,subprocess,time,tomllib,types,uuid
from pathlib import Path
import xml.etree.ElementTree as ET
try:import fcntl
except ImportError:fcntl=None

ROOT=Path('/opt/lighthouse-hub')
STATE=Path('/var/lib/lighthouse-replay-parts-formal-v5')
POLICY_FILE='replay-part-video-policy-v5.toml'
GRANT=Path('/etc/lighthouse-hub/replay-parts-workflow-launch-v5.json')
VERSION='replay-parts-formal-launch-operator-v5'
PILOT='6wHqfDmNF0U'
IMAGE='sha256:b6dd672929e46c1699c0b4281c7dbe047b099019bb9ae9ce832ce79a850eb94c'
PROOF_SHA='a735028c0e7e6b7a09bab290d9f4697cbe6b8a62609f18ac61ca9b29f0a318bb'
HELPER_SHA='e0e24d11d322f419db5a6092e1176b12d938b5577919c67977d28fe5d60813aa'
ENTRY=['python','-m','services.hub.replay_parts_worker_runtime_v5','--publish']

def need(value,code):
    if not value:raise ValueError(code)
def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def digest(value):return sha(wire(value))
def env_map(values):
    pairs=[v.split('=',1) for v in values];need(all(len(p)==2 for p in pairs),'INVALID_ENV')
    out=dict(pairs);need(len(out)==len(pairs),'DUPLICATE_ENV');return out

def formal_policy(raw):
    old=tomllib.loads(raw.decode('utf-8-sig'))
    need(old['version']=='replay-part-video-policy-v5' and old['enabled'] is True and
        old['source_scope']=='pilot_single' and old['acceptance_video_ids']==[PILOT] and
        old['automation_enabled'] is False,'EXACT_EXISTING_PILOT_REQUIRED')
    new={**old,'source_scope':'all_eligible_ended_since_operating_cutoff','acceptance_video_ids':[],
        'automation_enabled':True}
    need({k for k in new if new[k]!=old[k]}=={'source_scope','acceptance_video_ids','automation_enabled'},'POLICY_SCOPE_CHANGED')
    data=('\n'.join(k+' = '+('true' if v is True else 'false' if v is False else json.dumps(v,ensure_ascii=False))
        for k,v in new.items())+'\n').encode()
    need(tomllib.loads(data.decode())==new,'POLICY_SERIALIZATION_CHANGED')
    return new,data

def candidate(before):
    after=deepcopy(before);video=after['services']['video-worker']
    need(video['image']==IMAGE and video['command']==ENTRY and video['user']=='10001:10001','EXACT_CURRENT_VIDEO_REQUIRED')
    need(not any(v['target']==str(GRANT) for v in video['volumes']),'FORMAL_GRANT_ALREADY_MOUNTED')
    video['volumes'].append({'type':'bind','source':str(GRANT),'target':str(GRANT),'read_only':True,
        'bind':{'create_host_path':False}})
    reverse=deepcopy(after);reverse['services']['video-worker']['volumes'].pop()
    need(reverse==before,'ONLY_VIDEO_GRANT_MOUNT_MAY_CHANGE');return after

def runtime_fingerprint(info):
    return {'id':info['Id'],'image':info['Image'],'pid':info['State']['Pid'],'started_at':info['State']['StartedAt'],
        'running':info['State']['Running'],'env_sha256':digest(env_map(info['Config']['Env'])),
        'process_sha256':digest({k:info['Config'][k] for k in ('Cmd','Entrypoint','User','WorkingDir','Hostname')}),
        'host_config_sha256':digest(info['HostConfig']),'mounts_sha256':digest(info['Mounts'])}

def validate_qualification(proof,raw,xml,release):
    need(sha(raw)==PROOF_SHA==release['test_attestation']['sha256'],'EXACT_CURRENT_QUALIFICATION_REQUIRED')
    cases=list(ET.fromstring(xml).iter('testcase'))
    need(proof['version']=='replay-parts-retention-v5-ubuntu-tests' and proof['image']==IMAGE and
        proof['tests']==26 and len(cases)==26 and proof['test_groups']=={'tests.test_replay_parts_retention_v5':26} and
        all(c.attrib['classname']=='tests.test_replay_parts_retention_v5' and
            all(c.find(t) is None for t in ('failure','error','skipped')) for c in cases) and
        sha(xml)==proof['junit_sha256'],'QUALIFICATION_JUNIT_OR_IMAGE_CHANGED')
    need(all(proof[k]==0 for k in ('exit_code','failures','errors','skipped')) and proof['network']=='none' and
        all(proof[k] is True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate',
            'mounts_match_isolated_allowlist','mounted_sources_unchanged')) and
        all(proof[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed','timed_out')),
        'QUALIFICATION_FAILED_OR_NOT_ISOLATED')
    need(proof['source_files']==release['source_files'] and len(proof['source_files'])==141,'CURRENT_SOURCE_PROOF_CHANGED')

APP=r'''
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import hashlib,json,os,sqlite3,stat,time
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import encode
from services.hub.source_receipts import digest
from services.hub.replay_parts_v5 import ReplayPartVideoWeb,GROUP_JOB
from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5,MARKER,RELEASE,VERSION as RETENTION

def require(value,code):
    if not value:raise ValueError(code)

class ReadOnlyStore:
    def __init__(self,path):
        self.db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA query_only=ON');self.db.execute('BEGIN')
    def query(self,sql,args=()):return [dict(r) for r in self.db.execute(sql,args)]
    def get(self,ident):
        rows=self.query('SELECT * FROM jobs WHERE job_id=?',(ident,))
        if not rows:return None
        row=rows[0]
        for key in ('payload','result'):row[key]=json.loads(row[key])
        return row

def idle(store,*,check_processes=False):
    active=store.query("SELECT job_id FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)")
    require(not active,'ACTIVE_VIDEO_OR_LEASE_REQUIRED_TO_FINISH')
    if check_processes:
        unexpected=[]
        for item in Path('/proc').iterdir():
            if not item.name.isdigit() or int(item.name)==os.getpid():continue
            try:args=(item/'cmdline').read_bytes().split(b'\0');args=[v.decode('utf8','replace') for v in args if v]
            except (FileNotFoundError,ProcessLookupError,PermissionError):continue
            if not args:continue
            name=Path(args[0]).name
            if item.name=='1' and name in ('docker-init','tini'):continue
            if name.startswith('python') and args[1:]==['-m','services.hub.replay_parts_worker_runtime_v5','--publish']:continue
            unexpected.append({'pid':int(item.name),'executable':name})
        require(not unexpected,'VIDEO_SUBPROCESS_OR_PROFILE_OWNER_STILL_ACTIVE')
    return {'active_video_jobs':0,'processes_checked':check_processes}

def collect_acceptance(settings,store,group_id,*,expected_source='6wHqfDmNF0U'):
    retention=ReplayPartsRetentionV5(settings,store);owner,release_sha=retention._release()
    context=retention._context(group_id);public=retention._public(group_id)
    parent,source=context['parent'],context['source']
    require(parent['job_type']==GROUP_JOB and parent['source_id']==source['source_id']==expected_source,'EXACT_PILOT_GROUP_REQUIRED')
    policy=ReplayPartVideoWeb(settings,store).policy()
    require(policy['source_scope']=='pilot_single' and policy['acceptance_video_ids']==[expected_source] and
        policy['automation_enabled'] is False and parent['payload']['policy_sha256']==policy['_sha256'],'PILOT_POLICY_BINDING_CHANGED')
    marker=parent['result'].get(MARKER)
    require(marker is not None and marker==source['result'].get(MARKER),'ACTUAL_RETENTION_BOTH_MARKERS_REQUIRED')
    require(retention.index.is_file() and not retention.index.is_symlink(),'ACTUAL_RETENTION_LEDGER_REQUIRED')
    with closing(sqlite3.connect(retention.index.as_uri()+'?mode=ro',uri=True)) as index:
        index.execute('PRAGMA query_only=ON')
        operations=index.execute('SELECT id,plan_sha256,state,record FROM operations WHERE group_id=?',(group_id,)).fetchall()
    require(len(operations)==1 and operations[0][2]=='completed' and json.loads(operations[0][3])==marker,
        'ACTUAL_RETENTION_COMPLETION_REQUIRED')
    expected=[{k:f[k] for k in ('file','bytes','sha256')} for f in context['files']]
    require(marker['version']==RETENTION and marker['group_id']==group_id and marker['download_job_id']==source['job_id'] and
        marker['source_id']==expected_source and marker['plan_id']==operations[0][0] and
        marker['authorization_id']==owner['authorization_id'] and marker['owner_response_reference']==owner['owner_response_reference'] and
        marker['actual_owner_response']==owner['actual_owner_response'] and marker['deleted_candidates']==expected and
        type(marker['local_files_deleted']) is int and marker['local_files_deleted']==len(expected)>0 and
        marker['remote_video_deleted'] is False and marker['human_review'] is False and
        marker['evidence_files_deleted'] is False and all(not Path(f['file']).exists() for f in expected),
        'RETENTION_RECEIPT_OR_MEDIA_STATE_CHANGED')
    deleted=datetime.fromisoformat(marker['deleted_at'].replace('Z','+00:00')).timestamp()
    audits=store.query('SELECT at FROM audit WHERE principal=? AND action=? AND target=? AND outcome=?',
        (RETENTION,RETENTION,group_id,'all_parts_public_exact_local_media_deleted_metadata_and_bvs_retained'))
    require(any(deleted<=a['at']<=time.time() for a in audits),'ACTUAL_RETENTION_AUDIT_REQUIRED')
    first=parent['result']['replay_parts_first_completion_v5'];refs=[first]
    for part in context['completion']['parts']:
        for key in ('native_result_evidence','manager_observation','public_observation','public_screenshot'):
            ref=part['public_coverage'][key]
            if ref not in refs:refs.append(ref)
    require(all(Path(r['file']).is_relative_to(settings.root/'media') for r in refs),'PUBLIC_REFERENCE_SCOPE_CHANGED')
    return {'acceptance_parent_job_id':group_id,'source_id':expected_source,'download_job_id':source['job_id'],
        'target_site_uid':policy['target_site_uid'],'part_count':len(context['children']),
        'first_completion':first,'completed_group_result_sha256':digest(context['completion']['summary']),
        'public_parts':[{'part_index':p['part_index'],'job_id':p['job_id'],'external_id':p['external_id'],
            'original_accepted_at':p['original_accepted_at']} for p in public],
        'retention':{'plan_id':marker['plan_id'],'plan_sha256':operations[0][1],'receipt_sha256':digest(marker),
            'authorization_id':owner['authorization_id'],'deleted_candidates':expected,'completed_audit_present':True},
        'required_public_references':refs,'release_sha256':release_sha,
        'owner_references':{k:policy[k] for k in ('actual_owner_workflow_response','owner_response_reference','workflow_authorization_reference')},
        'database_rows_changed':getattr(getattr(store,'db',None),'total_changes',0)}

def app_main(group,check_processes):
    require(os.getuid()==10001,'APP_UID_REQUIRED');settings=Settings.load();store=ReadOnlyStore(settings.db_path)
    try:
        value=collect_acceptance(settings,store,group);value['idle']=idle(store,check_processes=check_processes)
        require(store.db.total_changes==0,'READ_ONLY_APPLICATION_PROBE_REQUIRED')
        print(encode(value))
    finally:store.db.close()
'''

def make_plan(*,accepted_at,acceptance,policy_raw,compose,video,peers,config_hashes,release,proof_sha,operator_sha):
    policy,new_raw=formal_policy(policy_raw)
    need(acceptance['source_id']==PILOT and acceptance['target_site_uid']==policy['target_site_uid'] and
        acceptance['owner_references']=={k:policy[k] for k in ('actual_owner_workflow_response','owner_response_reference','workflow_authorization_reference')},'OWNER_OR_TARGET_BINDING_CHANGED')
    grant={'version':'replay-parts-workflow-launch-v5','policy_sha256':sha(new_raw),
        'target_site_uid':policy['target_site_uid'],**acceptance['owner_references'],'accepted_at':accepted_at,
        'acceptance_parent_job_id':acceptance['acceptance_parent_job_id'],
        'completed_group_result_sha256':acceptance['completed_group_result_sha256'],
        'retention_confirmation':acceptance['retention'],
        'technical_qualification':{'image':IMAGE,'retention_proof_sha256':proof_sha,'source_files_sha256':digest(release['source_files'])}}
    plan={'version':VERSION,'accepted_at':accepted_at,'operator_sha256':operator_sha,'helper_sha256':HELPER_SHA,
        'expected_video':runtime_fingerprint(video),'peer_fingerprints':peers,'compose_sha256':sha(compose),
        'config_sha256':config_hashes,'old_policy_sha256':sha(policy_raw),'formal_policy_sha256':sha(new_raw),
        'acceptance':{k:v for k,v in acceptance.items() if k!='idle'},'grant':grant,
        'retention_proof_sha256':proof_sha,'core_image_changed':False,'new_owner_approval_claimed':False,
        'provider_requests':0,'media_deleted_by_operator':0,'queue_rows_changed_by_operator':0}
    return {**plan,'plan_sha256':digest(plan)},new_raw

def load_helper(path):
    path=Path(path);raw=path.read_bytes();need(sha(raw)==HELPER_SHA,'EXACT_FROZEN_HELPER_REQUIRED')
    module=types.ModuleType('frozen_v5_pilot_helper');module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__);module.protected(path);return module

def acceptance(d,cid,group,*,processes=False):
    data=d.run(['docker','exec','--user','10001:10001',cid,'python','-c',APP+"\nimport sys;app_main(sys.argv[1],sys.argv[2]=='true')",group,str(processes).lower()],timeout=120).stdout
    value=json.loads(data);need(value['database_rows_changed']==0,'READ_ONLY_PROBE_REQUIRED');return value

@contextmanager
def idle_locks(d):
    with d.idle_locks():
        handles=[]
        try:
            for name in ('bilibili-video-profile','heavy'):
                file=ROOT/'data/locks'/name/'observer.lock'
                need(file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)),'UNSAFE_IDLE_LOCK')
                fd=os.open(file,os.O_RDWR|os.O_NOFOLLOW);handles.append(fd);s=os.fstat(fd);p=file.stat()
                need(stat.S_ISREG(s.st_mode) and s.st_uid==10001 and s.st_nlink==1 and (s.st_dev,s.st_ino)==(p.st_dev,p.st_ino),'IDLE_LOCK_CHANGED')
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:
            for fd in reversed(handles):os.close(fd)

def preflight(d,image,folder,proof_folder,environment,refs):
    name='replay-formal-preflight-'+uuid.uuid4().hex
    args=['docker','run','--rm','--name',name,'--pull=never','--init','--network=none','--read-only',
        '--user','10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges:true',
        '--security-opt=seccomp='+str(ROOT/'config/browser-seccomp.json'),'--memory=1400m','--cpus=1',
        '--pids-limit=256','--shm-size=256m','--tmpfs=/tmp:rw,size=192m,mode=1777',
        '--tmpfs='+str(ROOT/'data')+':rw,size=128m,mode=0700,uid=10001,gid=10001',
        '--mount=type=bind,source='+str(folder/'snapshot.sqlite3')+',target=/run/source.sqlite3,readonly',
        '--mount=type=bind,source='+str(folder/'config')+',target='+str(ROOT/'config')+',readonly',
        '--mount=type=bind,source='+str(proof_folder)+',target=/run/qualifications/retention-v5,readonly',
        '--mount=type=bind,source='+str(folder/'launch.json')+',target='+str(GRANT)+',readonly']
    targets=set()
    for ref in refs:
        path=Path(ref['file']);need(path.is_relative_to(ROOT/'media') and path.resolve()==path and path not in targets,'PUBLIC_REFERENCE_SCOPE_CHANGED')
        raw=path.read_bytes();need(len(raw)==ref['bytes'] and sha(raw)==ref['sha256'],'PUBLIC_REFERENCE_CHANGED');targets.add(path)
        args+=['--mount=type=bind,source='+str(path)+',target='+str(path)+',readonly']
    need(set(environment)=={'DRY_RUN','HOME','HUB_CONFIG','HUB_ROOT','PYTHONDONTWRITEBYTECODE','XDG_CACHE_HOME','XDG_CONFIG_HOME','YOUTUBE_API_KEY_FILE'},'NARROW_PREFLIGHT_ENV_REQUIRED')
    for key,value in environment.items():args+=['--env='+key+'='+str(value)]
    try:result=d.run([*args,'--entrypoint=python',image,'-c',d.PRECHECK],timeout=180)
    except subprocess.TimeoutExpired:
        info=json.loads(d.run(['docker','inspect',name]).stdout)[0]
        need(info['Name']=='/'+name and info['Image']==image,'PREFLIGHT_CLEANUP_SCOPE_CHANGED')
        d.run(['docker','stop','--time','15',info['Id']],timeout=30);raise
    value=json.loads(result.stdout);need(value['claims']==value['external_requests']==value['live_database_writes']==0 and
        value['handlers']==36 and value['retention_release_valid'] is True,'FORMAL_PREFLIGHT_FAILED')
    return value

def verify_running(before,after,compose):
    need(after['Image']==before['Image']==IMAGE and after['State']['Running'],'EXPECTED_SAME_IMAGE_REQUIRED')
    need(env_map(after['Config']['Env'])==env_map(before['Config']['Env']),'ENV_CHANGED')
    for key in ('Cmd','Entrypoint','User','WorkingDir'):need(after['Config'][key]==before['Config'][key],'PROCESS_CONFIG_CHANGED')
    old,new=deepcopy(before['HostConfig']),deepcopy(after['HostConfig'])
    for value in (old,new):value.pop('Binds',None);value.pop('Mounts',None)
    need(old==new,'HOST_LIMITS_OR_RESTART_POLICY_CHANGED')
    mounts=deepcopy(after['Mounts']);matches=[m for m in mounts if m['Destination']==str(GRANT)]
    need(len(matches)==1 and matches[0]['Source']==str(GRANT) and matches[0]['Type']=='bind' and matches[0]['RW'] is False,'EXACT_ROOT_GRANT_MOUNT_REQUIRED')
    mounts.remove(matches[0]);need(mounts==before['Mounts'],'OTHER_MOUNT_CHANGED')
    host=compose['services']['video-worker'].get('hostname')
    need(after['Config']['Hostname']==(host or after['Id'][:12]),'HOSTNAME_CHANGED')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--deploy-helper',required=True)
    p.add_argument('--expected-cid',required=True);p.add_argument('--expected-image',default=IMAGE)
    p.add_argument('--acceptance-group',required=True);p.add_argument('--apply',action='store_true')
    p.add_argument('--plan-file');p.add_argument('--expected-plan-sha256');a=p.parse_args()
    need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_HOST_REQUIRED');os.umask(0o077)
    need(re.fullmatch('[a-f0-9]{64}',a.expected_cid) and a.expected_image==IMAGE and str(uuid.UUID(a.acceptance_group))==a.acceptance_group,'EXACT_CURRENT_SCOPE_REQUIRED')
    need(bool(a.plan_file)==bool(a.expected_plan_sha256)==a.apply,'SAVED_PLAN_REQUIRED_ONLY_FOR_APPLY')
    d=load_helper(a.deploy_helper);d.protected(Path(__file__).resolve());operator_sha=sha(Path(__file__).read_bytes())
    saved=json.loads(d.read(Path(a.plan_file))) if a.apply else None
    if saved:need(saved['plan_sha256']==a.expected_plan_sha256==digest({k:v for k,v in saved.items() if k!='plan_sha256'}),'EXPECTED_PLAN_DIGEST_REQUIRED')
    accepted_at=saved['accepted_at'] if saved else datetime.now(timezone.utc).isoformat()
    need(datetime.fromisoformat(accepted_at.replace('Z','+00:00')).timestamp()<=time.time()+1,'ACCEPTANCE_TIME_INVALID')
    video=d.inspect('video-worker');need(video['Id']==a.expected_cid and video['Image']==IMAGE and video['State']['Running'],'CURRENT_VIDEO_CHANGED')
    before_raw=d.read(ROOT/'compose.yaml');before=json.loads(before_raw);after=candidate(before)
    policy_raw=d.read(ROOT/'config'/POLICY_FILE);formal_policy(policy_raw)
    release_raw=d.read(ROOT/'config/replay-parts-retention-v5-release.json');release=json.loads(release_raw)
    mounts=[m for m in video['Mounts'] if m['Destination']=='/run/qualifications/retention-v5']
    need(len(mounts)==1 and mounts[0]['RW'] is False,'CURRENT_PROOF_RO_MOUNT_REQUIRED');proof_folder=Path(mounts[0]['Source'])
    proof_raw=d.read(proof_folder/'test-attestation.json');proof=json.loads(proof_raw)
    validate_qualification(proof,proof_raw,d.read(proof_folder/'results.xml'),release)
    config_names=set(d.FIXED_CONFIG)|set(d.NAMES)|{'browser-seccomp.json'}
    configs={n:d.read(ROOT/'config'/n) if n!='browser-seccomp.json' else (ROOT/'config'/n).read_bytes() for n in config_names}
    peers={n:runtime_fingerprint(d.inspect(n)) for n in d.PEERS}
    checked=acceptance(d,video['Id'],a.acceptance_group)
    need(checked['release_sha256']==sha(release_raw),'APPLICATION_RELEASE_DIFFERS')
    plan,new_policy=make_plan(accepted_at=accepted_at,acceptance=checked,policy_raw=policy_raw,compose=before_raw,
        video=video,peers=peers,config_hashes={n:sha(v) for n,v in configs.items()},release=release,proof_sha=sha(proof_raw),operator_sha=operator_sha)
    need(not GRANT.exists(),'EXISTING_FORMAL_GRANT_REQUIRES_RECONCILIATION');d.protected(GRANT.parent)
    if not a.apply:print(json.dumps(plan,ensure_ascii=False));return
    need(plan==saved,'CURRENT_STATE_DIFFERS_FROM_SAVED_PLAN')
    # Evidence-only staging starts after the explicit, exact-plan apply.
    if not STATE.exists():
        STATE.mkdir(mode=0o750);os.chown(STATE,0,10001);STATE.chmod(0o750)
    d.protected(STATE);folder=STATE/str(uuid.uuid4());folder.mkdir(mode=0o750);os.chown(folder,0,10001);folder.chmod(0o750)
    report={'version':VERSION,'state':'apply_intent','plan_sha256':plan['plan_sha256'],'image':IMAGE,
        'expected_cid':video['Id'],'accepted_owner_references_reused':True,'new_owner_approval_claimed':False}
    try:
        d.save(folder/'plan.json',wire(plan)+b'\n');d.save(folder/'report.json',wire(report)+b'\n')
        d.save(folder/'snapshot.sqlite3',d.run(['docker','exec','--user','10001:10001',video['Id'],'python','-c',d.SNAPSHOT],timeout=90).stdout)
        cfg=folder/'config';cfg.mkdir(mode=0o750);os.chown(cfg,0,10001);cfg.chmod(0o750)
        for n,raw in configs.items():d.save(cfg/n,new_policy if n==POLICY_FILE else configs['video-runtime-v5.toml'] if n=='hub.toml' else raw)
        d.save(folder/'launch.json',wire(plan['grant'])+b'\n')
        report['preflight']=preflight(d,IMAGE,folder,proof_folder,before['services']['video-worker']['environment'],checked['required_public_references'])
        d.save(folder/'report.json',wire(report)+b'\n')
        with idle_locks(d):
            need(runtime_fingerprint(d.inspect('video-worker'))==plan['expected_video'] and d.read(ROOT/'compose.yaml')==before_raw,'CURRENT_VIDEO_CHANGED_AT_IDLE')
            need(all(runtime_fingerprint(d.inspect(n))==fp for n,fp in peers.items()),'OTHER_SERVICE_CHANGED')
            need(all((ROOT/'config'/n).read_bytes()==raw for n,raw in configs.items()),'CONFIG_CHANGED')
            now_acceptance=acceptance(d,video['Id'],a.acceptance_group,processes=True)
            need({k:v for k,v in now_acceptance.items() if k!='idle'}==plan['acceptance'],'ACCEPTANCE_CHANGED_AT_IDLE')
            d.save(folder/'compose.before.json',before_raw,0o600);d.save(folder/'policy.before.toml',policy_raw)
            d.run(d.command('stop','--timeout','45','video-worker'),timeout=60)
            need(not json.loads(d.run(['docker','inspect',video['Id']]).stdout)[0]['State']['Running'],'OLD_VIDEO_STILL_RUNNING')
            need(not GRANT.exists(),'FORMAL_GRANT_APPEARED')
            d.save(GRANT,wire(plan['grant'])+b'\n');d.save(ROOT/'config'/POLICY_FILE,new_policy)
            d.save(ROOT/'compose.yaml',wire(after)+b'\n');report['state']='installed_restart_pending';d.save(folder/'report.json',wire(report)+b'\n')
        d.run(d.command('up','-d','--no-deps','--no-build','--pull','never','video-worker'),timeout=120)
        current=d.inspect('video-worker');verify_running(video,current,after)
        need(all(runtime_fingerprint(d.inspect(n))==fp for n,fp in peers.items()),'OTHER_SERVICE_CHANGED_AFTER_START')
        need(all((ROOT/'config'/n).read_bytes()==raw for n,raw in configs.items() if n!=POLICY_FILE),'OTHER_CONFIG_CHANGED_AFTER_START')
        need(d.read(ROOT/'config'/POLICY_FILE)==new_policy and d.read(GRANT)==wire(plan['grant'])+b'\n' and
            d.read(ROOT/'compose.yaml')==wire(after)+b'\n','INSTALLED_FORMAL_PLAN_CHANGED')
        report.update(state='formal_video_started_acceptance_pending',new_video_cid=current['Id'])
        # The existing worker's live heartbeat/ready checks remain actual reads;
        # no job is claimed by this operator to manufacture activity.
        code="import json,sqlite3;from services.hub.config import Settings;s=Settings.load();c=sqlite3.connect(s.db_path.as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');print(json.dumps(dict(c.execute(\"SELECT name,at FROM heartbeats WHERE name IN ('worker:video-main-v5','worker:video-producer-v5','worker:video-part-moderation-v5','worker:video-retention-v5')\"))))"
        started=datetime.fromisoformat(current['State']['StartedAt'].replace('Z','+00:00')).timestamp();deadline=time.monotonic()+180;first=None
        while True:
            beats=json.loads(d.run(['docker','exec','--user','10001:10001',current['Id'],'python','-c',code]).stdout)
            if set(beats)==set(d.BEATS) and all(started<=v<=time.time()+1 for v in beats.values()):
                if first is not None and all(beats[k]>first[k] for k in beats):break
                if first is None:first=beats.copy()
            need(time.monotonic()<deadline,'FORMAL_HEARTBEATS_NOT_ADVANCING');time.sleep(2)
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:
            ready=json.loads(response.read(8192));need(response.status==200 and ready['status']=='ready' and ready['stale_services']==[],'READY_REQUIRED')
        need(all(runtime_fingerprint(d.inspect(n))==fp for n,fp in peers.items()),'OTHER_SERVICE_CHANGED_DURING_ACCEPTANCE')
        report.update(state='formal_all_sources_active',heartbeats=beats,heartbeat_baseline=first,ready=ready,
            other_services_unchanged=True,core_image_changed=False,queue_rows_changed_by_operator=False)
        d.save(folder/'report.json',wire(report)+b'\n');print(json.dumps({'state':report['state'],'report':str(folder/'report.json')}))
    except BaseException as error:
        report.update(state='requires_reconciliation',error_class=type(error).__name__)
        d.save(folder/'report.json',wire(report)+b'\n');raise

if __name__=='__main__':main()
