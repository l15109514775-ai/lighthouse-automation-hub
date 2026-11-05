"""Video-only two-replay repair; exact new qualification is mandatory. Default stages and verifies an offline plan.

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

ROOT=Path('/opt/lighthouse-hub');STATE=Path('/var/lib/lighthouse-two-replay-repair-v6')
CURRENT='7d01f1363649291076f79550af633040a4a3a33146120b143ee6329718d38a6e'
OLD_IMAGE='sha256:f9eb4449dc9c8e410935329e21ec147a55dc7ba80056db8f8eb27fd5551d82be'
IMAGE='sha256:2bf4c8496b6c4e74d660fd2658812a55af2665fc2dc331421d2cb8e3334d9891'
DEPLOY_SHA='e0e24d11d322f419db5a6092e1176b12d938b5577919c67977d28fe5d60813aa'
HELPER_SHA='4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
QUALIFIED=Path('/var/lib/lighthouse-backup-ops/two-replay-repair-v6-tests/2873e728-b1ed-4569-9de6-d44a72524105/test-result.json')
QUALIFIED_SHA='b92d7591f8db007673d1f3be52bddb4d86df9f1025371415683bced4d18e55a4'
PROOF=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/8c1a9103-355f-47e3-a146-2424885b3280/test-attestation.json')
PROOF_SHA='06a6d93aa663e74b06af5b135727d4eb4f4483396e860bf0adec9ea3df9f2f17'
OLD_PROOF=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/8f88c9ff-396c-44d9-a404-acf9335d3a09')
OLD_PROOF_SHA='7d26e484d276ad7fa53e5e4573b2c672da2e942bea3e38d6462d1777e8695e22'
OWNER=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/owner-authorization-1fb26fc9.json')
OWNER_SHA='8869d3ad9f68c317030ebbfdf5211075a624563e0adf9f653317a03747910eb9'
SOURCES={'adapters/replay_later_result_lookup_v2.mjs': 'c8f8956c5505e7a5b37f7f757e66b0733992868ee1b0ec54d18c10ff80df1b84', 'adapters/replay_native_driver.mjs': '15baf8c6065acde7b47171f8ce82796c11204d9c6c8b707a36c28af0ac3b42cc', 'adapters/ytdlp_highest_v2.py': '8dc05175a9eba67ab881a40d19c90ab9231c15ae453d559859d62d087061282a', 'services/hub/replay_part_moderation_v5.py': 'b3b605f2134f03bd918dc951daa4706796f4deb7f665edefa9bd75527c8fd6fb'}
PARENT70_SHA='309d2b2228b29f26f8edfb702b74d055e36076e1ffba12f7f15782edf8419f6a'
FORMAL_HELPER_SHA='cc95c94f5db8510f3bed0307a7e5fb75506738fc4ca54876f125dbaed46a2417'
GRANT=Path('/etc/lighthouse-hub/replay-parts-workflow-launch-v5.json')
GROUPS={'tests.ops.test_qualify_two_replay_repair_v6': 11, 'tests.ops.test_recover_streamer-b_draft_v6': 51, 'tests.ops.test_resume_streamer-b_range_v1': 16, 'tests.test_replay_draft_lookup_v5': 11, 'tests.test_replay_draft_recovery_v5': 8, 'tests.test_replay_later_lookup_v2': 4, 'tests.test_replay_later_manager_pending_v5': 9, 'tests.test_replay_part_draft_persistence_v5': 4, 'tests.test_replay_part_moderation_v5': 23, 'tests.test_replay_part_native_v5': 13, 'tests.test_replay_post_submit_page_v5': 6, 'tests.test_youtube_highest_v2': 41, 'tests.test_ytdlp_highest_transport_v2': 12, 'tests.test_ytdlp_highest_v2': 8, 'tests.test_ytdlp_range_diagnostics_v1': 37}
SOURCE_COUNT=889
SOURCE_COMMIT='3f39f56f9b6902020cff32ec23e920f9a83c9029'
ARCHIVE_SHA='980abd885f33f94f9e1781762f4dfccda740064f2221e7e14e3a09864edb945d'
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

def normalized_host(value):
    result=deepcopy(value);result.pop('Binds',None);result.pop('Mounts',None)
    # Docker serializes an unset DNS option as either null or an empty list.
    # Explicit nonempty DNS lists keep their values and resolution order.
    for name in ('Dns','DnsOptions','DnsSearch'):
        if name in result and result[name] is None:result[name]=[]
    # These three fields are unordered sets. Every other array retains order.
    for name in ('CapAdd','CapDrop','SecurityOpt'):
        items=result.get(name)
        if items is None:continue
        need(type(items) is list and all(type(v) is str for v in items) and len(items)==len(set(items)),'INVALID_HOST_OPTION_SET')
        if name=='SecurityOpt':
            items=['seccomp='+wire(json.loads(v[8:])).decode() if v.startswith('seccomp=') else v for v in items]
        result[name]=sorted(items)
    return result

def mount_map(value):
    need(type(value) is list and all(type(v) is dict and type(v.get('Destination')) is str for v in value),'INVALID_MOUNT_LIST')
    result={v['Destination']:deepcopy(v) for v in value};need(len(result)==len(value),'DUPLICATE_MOUNT_DESTINATION');return result

def runtime_differences(old,new,service):
    fields=[]
    if env_map(old['Config']['Env'])!=env_map(new['Config']['Env']):fields.append('Config.Env')
    for key in ('Cmd','User','Entrypoint','WorkingDir'):
        if old['Config'][key]!=new['Config'][key]:fields.append('Config.'+key)
    host=service.get('hostname')
    if old['Config']['Hostname']!=(host or old['Id'][:12]) or new['Config']['Hostname']!=(host or new['Id'][:12]):fields.append('Config.Hostname')
    before,after=normalized_host(old['HostConfig']),normalized_host(new['HostConfig'])
    fields.extend('HostConfig.'+k for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k))
    mounts=mount_map(new['Mounts']);proof=mounts.get('/run/qualifications/retention-v5')
    if not proof or proof.get('Type')!='bind' or proof.get('Source')!=str(PROOF.parent) or proof.get('RW') is not False:fields.append('Mounts.retention_proof')
    else:
        proof['Source']=str(OLD_PROOF)
        if mounts!=mount_map(old['Mounts']):fields.append('Mounts.other')
    return fields

def verify_runtime(old,new,service):
    need(new['Image']==IMAGE and new['State']['Running'] is True,'NEW_VIDEO_NOT_RUNNING')
    fields=runtime_differences(old,new,service)
    if fields:
        error=ValueError('RUNTIME_CONFIGURATION_CHANGED');error.differing_fields=fields;raise error

def save_runtime_snapshot(d,folder,info,name):
    need(name in ('runtime-before.full.json','runtime-after.full.json'),'SNAPSHOT_NAME_REQUIRED')
    file=folder/name;raw=wire(info)+b'\n';d.save(file,raw,0o600);s=file.stat()
    need(s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o600 and d.read(file)==raw,'PRIVATE_ROOT_RUNTIME_SNAPSHOT_REQUIRED')
    # Never include the actual Env values in printed/ordinary reports.
    return {'file':str(file),'sha256':sha(raw),'bytes':len(raw),'mode':'0600','owner_uid':0}

def paused_snapshot(file):
    with sqlite3.connect('file:'+str(file)+'?mode=ro&immutable=1',uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()
        need(row and row['paused']==1,'MAINTENANCE_PAUSE_REQUIRED')
        rows=[dict(v) for v in db.execute("SELECT * FROM jobs WHERE queue='video' ORDER BY job_id")]
        need(not any(v['state']=='processing' or v['lease_owner'] is not None or v['lease_until'] is not None for v in rows),'ACTIVE_VIDEO_REFUSED')
        return sha(wire(rows))

def validate_release_change(prior,release):
    need(prior['test_attestation']['sha256']==OLD_PROOF_SHA and prior['authorization_sha256']==OWNER_SHA and
        set(prior['source_files'])==set(release['source_files']) and
        {n for n in prior['source_files'] if prior['source_files'][n]!=release['source_files'][n]}==set(SOURCES) and
        all(release['source_files'].get(n)==v for n,v in SOURCES.items()),'ONLY_QUALIFIED_REPAIR_SOURCES_MAY_CHANGE')

def activation_constants_ready():
    return (re.fullmatch('sha256:[a-f0-9]{64}',IMAGE) and all(re.fullmatch('[a-f0-9]{64}',v) for v in (QUALIFIED_SHA,PROOF_SHA,ARCHIVE_SHA)) and
        re.fullmatch('[a-f0-9]{40}',SOURCE_COMMIT) and type(SOURCE_COUNT) is int and SOURCE_COUNT>=877 and bool(GROUPS) and len(SOURCES)==4 and all(re.fullmatch('[a-f0-9]{64}',v) for v in SOURCES.values()))

def validate_image_lineage(original,candidate):
    need(original['Id']==OLD_IMAGE and candidate['Id']==IMAGE and
        len(original['RootFS']['Layers'])==5 and len(candidate['RootFS']['Layers'])==6 and
        candidate['RootFS']['Layers'][:-1]==original['RootFS']['Layers'],'QUALIFIED_PARENT_PLUS_EXACT_COPY_LAYER_REQUIRED')
    need(candidate['Config']==original['Config'] and all(candidate.get(k)==original.get(k) for k in ('Os','Architecture','Variant')),
        'COMPLETE_IMAGE_CONFIG_OR_PLATFORM_CHANGED')

def validate_qualifications(m,draw,dxml,praw,pxml,old26):
    need(activation_constants_ready(),'ACTUAL_TWO_REPLAY_REPAIR_AND26_STILL_PENDING')
    need(sha(draw)==QUALIFIED_SHA and sha(praw)==PROOF_SHA,'EXACT_ACTUAL_QUALIFICATION_REQUIRED')
    delta=json.loads(draw);proof=json.loads(praw);count=sum(GROUPS.values())
    need(delta['version']=='two-replay-repair-ubuntu-v6' and delta['image']==IMAGE and delta['base_image']==OLD_IMAGE and
        delta['source_commit']==SOURCE_COMMIT and delta['archive_sha256']==ARCHIVE_SHA and delta['passed'] is True and
        delta['tests']==count and delta['test_groups']==GROUPS and m.junit(dxml,count)==GROUPS and sha(dxml)==delta['junit_sha256'] and
        delta['parent_sha256']==PARENT70_SHA and delta['parent_retention_sha256']==OLD_PROOF_SHA,'ACTUAL_TWO_REPLAY_REPAIR_QUALIFICATION_REQUIRED')
    need(delta['source_before']==delta['source_after']==delta['source_hashes'] and len(delta['source_hashes'])==SOURCE_COUNT and
        all(delta['source_hashes'].get(n)==v for n,v in SOURCES.items()) and
        delta['source_layer_delta']==1 and
        delta['resource_limits_passed'] is True and delta['sources_unchanged'] is True and delta['production_all_five_unchanged'] is True and
        delta['network']=='none' and all(delta[k] is False for k in ('production_data_mounted','production_profile_mounted','credentials_injected','oom_killed','timed_out')),
        'ACTUAL_TWO_REPLAY_REPAIR_RUNTIME_REQUIRED')
    manifest=delta['source_manifest'];files=manifest['files'];base_files=delta['manifest']['base_source_manifest']['files'];prior=old26['source_files']
    prior_selected={n for n in base_files if n.startswith(('services/hub/','adapters/')) and n.endswith('.py')}|set(m.NODE_SOURCES)
    need(prior_selected==set(prior) and len(prior)==141 and all(base_files[n]['sha256']==v for n,v in prior.items()),'EXACT_QUALIFIED_PARENT_RUNTIME_REQUIRED')
    selected={n for n in files if n.startswith(('services/hub/','adapters/')) and n.endswith('.py')}|set(m.NODE_SOURCES)
    need(selected==set(prior) and len(selected)==141,'EXACT_SOURCE_SCOPE_REQUIRED')
    current={n:files[n]['sha256'] for n in selected}
    need(delta['manifest']['production_runtime_changed_files']==sorted(SOURCES) and
        {n for n in current if current[n]!=prior[n]}==set(SOURCES) and all(current[n]==v for n,v in SOURCES.items()),'ACTUAL_REPAIR_SOURCE_SCOPE_REQUIRED')
    need(proof['version']==m.VERSION and proof['image']==IMAGE and proof['tests']==26 and
        proof['qualified_parent_sha256']==QUALIFIED_SHA and proof['source_files']==current and proof['source_archive_sha256']==ARCHIVE_SHA and
        proof['source_commit']==SOURCE_COMMIT and proof['source_manifest_sha256']==sha(wire(manifest)) and
        proof['qualification_lineage']['two_replay_repair_sha256']==QUALIFIED_SHA and
        proof['qualification_lineage']['prior_lineage']==old26['qualification_lineage'] and
        proof['qualification_lineage']['runtime_changed_files']==sorted(SOURCES),'ACTUAL_26_SOURCE_REQUIRED')
    need(sha(pxml)==proof['junit_sha256'] and m.junit(pxml,26,group=m.GROUP)==proof['test_groups'],'ACTUAL_26_JUNIT_REQUIRED')
    need(all(proof[k]==0 for k in ('exit_code','failures','errors','skipped')) and proof['network']=='none' and
        all(proof[k] is True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate',
            'mounts_match_isolated_allowlist','mounted_sources_unchanged','production_all_five_unchanged')) and
        all(proof[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed','timed_out')),
        'ACTUAL_26_ISOLATION_REQUIRED')
    return delta,proof

APP=r'''import hashlib,json,os,sqlite3
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5
from services.hub.replay_parts_v5 import ReplayPartGroupsV5,ReplayPartVideoWeb
from services.hub.replay_parts_v5 import reference_body
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
  store=Store(s.db_path);policy=ReplayPartVideoWeb(s,store).policy();grant=ReplayPartGroupsV5(s,store).require_launch(policy)
  assert policy['source_scope']=='all_eligible_ended_since_operating_cutoff' and policy['automation_enabled'] is True
  parent=store.get(grant['acceptance_parent_job_id']);first=parent['result']['replay_parts_first_completion_v5']
  completion=reference_body(s.root,first,expected=s.root/'media'/parent['job_id']/'replay-parts-v5/first-completion.json');refs=[first]
  for part in completion['parts']:
   for key in ('native_result_evidence','manager_observation','public_observation','public_screenshot'):
    ref=part['public_coverage'][key]
    if ref not in refs:refs.append(ref)
  result.update(release_sha256=hashlib.sha256(raw).hexdigest(),runtime_source_count=len(release['source_files']),no_live_child=True,
   formal_launch_verified=True,acceptance_parent_job_id=parent['job_id'],required_public_references=refs)
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
    p.add_argument('--formal-helper',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_HOST_REQUIRED');os.umask(0o077)
    need(activation_constants_ready(),'ACTUAL_TWO_REPLAY_REPAIR_AND26_STILL_PENDING')
    d=load(a.deploy_helper,DEPLOY_SHA,'frozen_pilot_deployer');m=load(a.retention_helper,HELPER_SHA,'frozen_retention276');formal=load(a.formal_helper,FORMAL_HELPER_SHA,'frozen_formal_preflight')
    draw=m.read(QUALIFIED,protected=True);praw=m.read(PROOF,protected=True)
    old_raw=m.read(OLD_PROOF/'test-attestation.json',protected=True);need(sha(old_raw)==OLD_PROOF_SHA,'EXACT_CURRENT_RETENTION_REQUIRED')
    delta,proof=validate_qualifications(m,draw,m.read(Path(json.loads(draw)['junit_file']),protected=True),praw,m.read(PROOF.parent/'results.xml',protected=True),json.loads(old_raw))
    old_image=m.inspect_image(OLD_IMAGE);image=m.inspect_image(IMAGE);validate_image_lineage(old_image,image)
    authorization=json.loads(m.read(OWNER,protected=True));need(m.owner(authorization)==OWNER_SHA,'ORIGINAL_OWNER_REQUIRED')
    release=m.release_template(authorization,proof,praw);release_raw=wire(release)+b'\n'
    old=d.inspect('video-worker');need(old['Id']==CURRENT and old['Image']==OLD_IMAGE and old['State']['Running'],'CURRENT_VIDEO_CHANGED')
    before_raw=d.read(ROOT/'compose.yaml');before=json.loads(before_raw);after=candidate(before,PROOF.parent)
    peers={name:fingerprint(d.inspect(name)) for name in d.PEERS}
    configs={name:d.read(ROOT/'config'/name) for name in (*d.FIXED_CONFIG,*d.NAMES)}
    grant_raw=d.read(GRANT);grant_mount=[v for v in old['Mounts'] if v['Destination']==str(GRANT)]
    need(len(grant_mount)==1 and grant_mount[0]['Source']==str(GRANT) and grant_mount[0]['Type']=='bind' and grant_mount[0]['RW'] is False,'CURRENT_FORMAL_GRANT_RO_REQUIRED')
    prior=json.loads(configs[m.RELEASE]);validate_release_change(prior,release)
    m.root_directory(STATE);folder=m.root_directory(STATE/str(uuid.uuid4()))
    report={'version':'two-replay-repair-cutover-v6','state':'plan_only','phase':'snapshot','image':IMAGE,'old_cid':CURRENT,
        'qualification_sha256':QUALIFIED_SHA,'formal_grant_sha256':sha(grant_raw),'retention26_sha256':PROOF_SHA,'other_four':peers,'pause_preserved':True,
        'image_build_strategy':'qualified-parent-plus-delta-v1','source_layer_delta':1,'parent_rootfs_layers':5,'candidate_rootfs_layers':6,
        'job_retry_or_claim':False,'owner_window_touched':False,'full_ready_deferred_until_root_unpause':True}
    try:
        report['private_runtime_snapshot']=save_runtime_snapshot(d,folder,old,'runtime-before.full.json')
        d.save(folder/'snapshot.sqlite3',d.host_snapshot());paused_snapshot(folder/'snapshot.sqlite3')
        d.save(folder/'compose.before.json',before_raw,0o600);d.save(folder/'release.before.json',configs[m.RELEASE])
        d.save(folder/'runtime-before.filtered.json',wire(fingerprint(old))+b'\n')
        cfg=m.root_directory(folder/'config');private={**configs,'hub.toml':configs['video-runtime-v5.toml'],m.RELEASE:release_raw}
        seccomp=m.read(ROOT/'config/browser-seccomp.json');need(sha(seccomp)==delta['source_manifest']['files']['config/browser-seccomp.json']['sha256'],'SECCOMP_CHANGED')
        private['browser-seccomp.json']=seccomp
        for name,value in private.items():d.save(cfg/name,value)
        d.save(folder/'browser-seccomp.json',seccomp);report['phase']='preflight'
        d.save(folder/'launch.json',grant_raw);initial=probe(d,CURRENT,full=True)
        need(initial['formal_launch_verified'] is True,'CURRENT_FORMAL_LAUNCH_REQUIRED')
        report['preflight']=formal.preflight(d,IMAGE,folder,PROOF.parent,before['services']['video-worker']['environment'],initial['required_public_references'])
        with idle_locks(d):
            need(fingerprint(d.inspect('video-worker'))==fingerprint(old) and d.read(ROOT/'compose.yaml')==before_raw,'CURRENT_VIDEO_CHANGED_AT_IDLE')
            need(all(fingerprint(d.inspect(n))==v for n,v in peers.items()),'OTHER_SERVICE_CHANGED')
            need(all(d.read(ROOT/'config'/n)==v for n,v in configs.items()) and d.read(GRANT)==grant_raw,'CONFIG_CHANGED')
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
        new=d.inspect('video-worker');report['private_runtime_after_snapshot']=save_runtime_snapshot(d,folder,new,'runtime-after.full.json')
        verify_runtime(old,new,after['services']['video-worker'])
        checked=probe(d,new['Id'],full=True);need(checked['release_sha256']==sha(release_raw) and checked['runtime_source_count']==141 and checked['formal_launch_verified'] is True,'INSTALLED_SOURCE_RELEASE_REQUIRED')
        started=datetime.fromisoformat(new['State']['StartedAt'].replace('Z','+00:00')).timestamp();deadline=time.monotonic()+180;first=None;report['phase']='paused_heartbeats'
        while True:
            check=probe(d,new['Id']);beats=check['heartbeats']
            if advancing(beats,started,first,time.time()):break
            if set(beats)==REQUIRED_BEATS and all(started<=v<=time.time()+1 for v in beats.values()) and first is None:first=beats.copy()
            need(time.monotonic()<deadline,'PAUSED_HEARTBEATS_NOT_ADVANCING');time.sleep(2)
        need(all(fingerprint(d.inspect(n))==v for n,v in peers.items()),'OTHER_SERVICE_CHANGED_AFTER_START')
        need(all(d.read(ROOT/'config'/n)==v for n,v in configs.items() if n!=m.RELEASE) and d.read(GRANT)==grant_raw,'OTHER_CONFIG_CHANGED_AFTER_START')
        need(d.read(ROOT/'compose.yaml')==wire(after)+b'\n' and d.read(ROOT/'config'/m.RELEASE)==release_raw,'INSTALLED_BYTES_CHANGED')
        report.update(state='two_replay_repair_active_paused',phase='completed',new_cid=new['Id'],heartbeats=beats,heartbeat_baseline=first,
            installed_runtime=checked,other_services_unchanged=True,pause_preserved=True)
        d.save(folder/'report.json',wire(report)+b'\n');print(json.dumps({'state':report['state'],'report':str(folder/'report.json'),'new_cid':new['Id']}))
    except BaseException as error:
        code=getattr(error,'code',None)
        if isinstance(error,ValueError) and len(error.args)==1:code=error.args[0]
        if not isinstance(code,str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,95}',code):code=None
        report.update(state='requires_reconciliation',error_class=type(error).__name__,error_code=code,runtime_difference_fields=getattr(error,'differing_fields',[]))
        d.save(folder/'report.json',wire(report)+b'\n');raise

if __name__=='__main__':main()
