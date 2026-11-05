"""One qualified Highest pilot: default exact plan, explicit saved-plan apply.

Run as app10001 in the isolated qualified-current45 one-shot container. This
script never installs code, changes Compose, grants formal launch, or submits.
"""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,sys,uuid
import xml.etree.ElementTree as ET
sys.path.insert(0,'/opt/lighthouse-hub')

from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.archive_source_retention_v2 import _root_protected
from services.hub.pilot_published_retention_v2 import PilotPublishedRetentionV2,JOB,DOWNLOAD,SOURCE,BV,ATTEMPT,PARENT
from services.hub.video_source_retention_v2 import PRINCIPAL as RETENTION_PRINCIPAL,PUBLISHED
from services.hub.short_video_web import PRINCIPAL as LOOKUP_PRINCIPAL
from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb,highest_replay_manifest
from services.hub.replay_video_browser import ReplayVideoBrowser

ORIGINAL_COVERAGE='b4c1429958b6d376a34b85ce55246f346ad49e65912dad5915ca6e59c67cc2ee'
SAMPLES=Path('/opt/lighthouse-hub/media/multipart-v4-native-real-samples-f78cd998-5114-4a84-a88e-e3b7f02bd7ed/samples.json')
SAMPLES_SHA='abdbf1ab1a007a13f51c44db30d8279335dc70b01f8d0823fae1e73a97b5722c'
NAMES={'video-selection.json','cover-completion.json','private-draft-save.json','public-submit.json'}

def fail(code):raise HubError(code,review=True)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def safe(file):
    file=Path(file)
    if not file.is_absolute() or file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail('PILOT_CLEANUP_UNSAFE_PATH')
    return file
def read(file,maximum=1_000_000):
    file=safe(file);fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not 0<before.st_size<=maximum:fail('PILOT_CLEANUP_SIZE_LIMIT')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    if len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):fail('PILOT_CLEANUP_FILE_CHANGED')
    return raw
def reference(file,maximum=1_000_000):
    raw=read(file,maximum);return {'file':str(file),'bytes':len(raw),'sha256':sha(raw)}
def write(file,raw):
    file=safe(file);fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    if os.name=='nt':return  # Pure owned file tests; the live entry is POSIX-only.
    fd=os.open(file.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)
def json_write(file,value):
    raw=encode(value).encode()
    if file.exists():
        if read(file,2_000_000)!=raw:fail('PILOT_CLEANUP_RECEIPT_ALREADY_DIFFERENT')
        return
    write(file,raw)

def validate_mode(apply,plan_file,plan_sha):
    if apply and not (plan_file and plan_sha) or not apply and (plan_file or plan_sha):fail('PILOT_CLEANUP_EXACT_PLAN_REQUIRED')

def qualify(root,*,file,checksum):
    file=safe(file)
    if not isinstance(checksum,str) or len(checksum)!=64 or any(c not in 'abcdef0123456789' for c in checksum):fail('PILOT_CLEANUP_QUALIFICATION_REQUIRED')
    _root_protected(file);raw=read(file,2_000_000)
    if sha(raw)!=checksum:fail('PILOT_CLEANUP_QUALIFICATION_REQUIRED')
    v=json.loads(raw);junit=Path(v['junit_file']);_root_protected(junit);jraw=read(junit,2_000_000)
    cases=ET.fromstring(jraw).findall('.//testcase');groups={}
    for case in cases:groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
    if (v.get('version')!=PilotPublishedRetentionV2.TEST_VERSION or
            v.get('tests')!=43 or len(cases)!=43 or groups!=PilotPublishedRetentionV2.TEST_GROUPS or sha(jraw)!=v['junit_sha256'] or
            any(case.find(t) is not None for case in cases for t in ('error','failure','skipped')) or
            any(v.get(k)!=0 for k in ('exit_code','failures','errors','skipped')) or
            any(v.get(k) is not False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed')) or
            any(v.get(k) is not True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','mounted_sources_unchanged')) or
            v.get('network')!='none' or v.get('current_runtime_parent_archive_sha256')!=PARENT or
            v.get('qualification_scope')!='highest_pilot_current45_only_natural_not_qualified' or
            v.get('manifest',{}).get('version')!='published-pilot-current45-retention43-candidate-v3'):fail('PILOT_CLEANUP_QUALIFICATION_REQUIRED')
    expected=v['source_files']
    allowed=set(PilotPublishedRetentionV2.RELEASE_EXTRA_SOURCES)|{'services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py'}
    if set(expected)!=allowed or len(expected)!=50 or any(sha(read(safe(root/name),500000))!=h for name,h in expected.items()):fail('PILOT_CLEANUP_QUALIFIED_SOURCE_MISMATCH')
    if v['current_runtime_source_sha256']!={n:expected[n] for n in v['manifest']['runtime_source_sha256']} or len(v['current_runtime_source_sha256'])!=45:fail('PILOT_CLEANUP_QUALIFICATION_REQUIRED')
    return v,{'file':str(file),'sha256':checksum,'junit_sha256':sha(jraw)}

def owner_request(file):
    file=safe(file);_root_protected(file);v=json.loads(read(file,16384))
    if set(v)!={'actual_owner_response','owner_response_reference'} or any(not isinstance(x,str) or not x.strip() or len(x)>2048 for x in v.values()):fail('PILOT_CLEANUP_REAL_OWNER_REQUEST_REQUIRED')
    text=v['actual_owner_response']
    if any(s not in text for s in ('异地备份','录播','B站','保留')):fail('PILOT_CLEANUP_REAL_OWNER_REQUEST_REQUIRED')
    return v

def snapshot(retention):
    job,binding,state,source,pub=retention._context(JOB)
    if retention.store.query("SELECT 1 FROM jobs WHERE queue='video' AND state='processing'"):fail('PILOT_CLEANUP_WORKER_ACTIVE')
    root=retention.root/'media'/JOB;folder=safe(root/'replay-native-intents')
    if {p.name for p in folder.iterdir()}!=NAMES:fail('PILOT_CLEANUP_INTENTS_CHANGED')
    intents={p.name:reference(p,100000) for p in folder.iterdir()}
    unknown=root/'replay-browser-highest-v2'/ATTEMPT/'public-result.json'
    if json.loads(read(unknown))!={'state':'unknown','submitted':None,'reconcile_required':True}:fail('PILOT_CLEANUP_ORIGINAL_UNKNOWN_CHANGED')
    return {'job':job,'binding':binding,'state':state,'source':source,'publication':pub,'intents':intents,'unknown_result':reference(unknown)}

def assert_refresh_preserved(before,after):
    if before['binding']!=after['binding'] or before['source']!=after['source'] or before['intents']!=after['intents'] or before['unknown_result']!=after['unknown_result']:fail('PILOT_CLEANUP_LOOKUP_CHANGED_BINDING')
    for key in before['job']:
        if key not in ('result','updated_at','stage') and before['job'][key]!=after['job'][key]:fail('PILOT_CLEANUP_LOOKUP_CHANGED_JOB')
    old=deepcopy(before['job']['result']);new=deepcopy(after['job']['result'])
    for result in (old,new):
        state=result['replay_highest_video_web_v2'];state.pop('cleanup_coverage',None);state.pop('result_observation',None)
    if old!=new:fail('PILOT_CLEANUP_LOOKUP_CHANGED_HISTORY')
    if {k:v for k,v in before['publication'].items() if k!='updated_at'}!={k:v for k,v in after['publication'].items() if k!='updated_at'}:fail('PILOT_CLEANUP_LOOKUP_CHANGED_PUBLICATION')

def fresh_callback(settings,store,retention,operation,*,browser=None):
    service=HighestReplayVideoWeb(settings,store);before=snapshot(retention);fingerprint=encode(before)
    def guard():
        if encode(snapshot(retention))!=fingerprint:fail('PILOT_CLEANUP_LOOKUP_CONTEXT_CHANGED')
        if service.policy()['_sha256']!=before['binding']['policy_sha256']:fail('PILOT_CLEANUP_POLICY_CHANGED')
    def refresh():
        guard();duration=before['source']['result'][service.DOWNLOAD_STATE]['binding']['duration']
        manifest=highest_replay_manifest(settings,store,before['job'],before['binding'],before['state'],ATTEMPT,duration_seconds=duration,require_cover=False)
        with (browser or ReplayVideoBrowser(settings)).session(manifest,assert_lease=guard) as session:
            value=session.operation('lookup',external_id=BV);guard()
        observation=value.get('result_observation')
        if not isinstance(observation,dict) or observation.get('state')!='completed' or observation.get('external_id')!=BV or observation.get('public_accessible') is not True:fail('PILOT_CLEANUP_FRESH_PUBLIC_LOOKUP_REQUIRED')
        service.record_result(LOOKUP_PRINCIPAL,JOB,observation,lookup=True)
        after=snapshot(retention);assert_refresh_preserved(before,after)
        json_write(operation/'fresh-lookup.json',{'version':'pilot-completed-public-refresh-v2','before_job_sha256':sha(encode(before['job']).encode()),
            'after_job_sha256':sha(encode(after['job']).encode()),'public_observation':observation,'files_selected':0,'submits':0,'worker_lease_fabricated':False})
    return refresh

def retained_samples(root,binding,*,file=SAMPLES,checksum=SAMPLES_SHA):
    raw=read(file,200000);v=json.loads(raw)
    if (sha(raw)!=checksum or v.get('version')!='real-source-private-multipart-samples-v4' or v.get('source_id')!=SOURCE or v.get('source_path')!=binding['file'] or
            v.get('source_bytes')!=binding['file_bytes'] or v.get('source_sha256')!=binding['artifact_sha256'] or
            v.get('method')!='ffmpeg_stream_copy' or v.get('private_upload_only') is not True or v.get('public_submission_allowed') is not False or
            v.get('formal_splitter_acceptance_claimed') is not False or len(v.get('parts',[]))!=2):fail('PILOT_CLEANUP_RETAINED_SAMPLES_REQUIRED')
    records=[]
    for part in v['parts']:
        path=safe(Path(part['path']))
        if path.parent!=file.parent or path==Path(binding['file']) or path.suffix!='.mp4':fail('PILOT_CLEANUP_RETAINED_SAMPLES_REQUIRED')
        actual=reference(path,50_000_000)
        if actual['bytes']!=part['bytes'] or actual['sha256']!=part['sha256']:fail('PILOT_CLEANUP_RETAINED_SAMPLES_REQUIRED')
        records.append(actual)
    return {'manifest':{'file':str(file),'sha256':checksum,'bytes':len(raw)},'parts':records,'whole_sample_directory_retained':True}

def preserve_before(root,before,operation,original=None):
    file=Path(before['state']['cleanup_coverage']['evidence']['file']);raw=read(file,65536)
    write(operation/'coverage-before.json',raw)
    original_raw=raw if sha(raw)==ORIGINAL_COVERAGE else read(original,65536) if original is not None else b''
    if sha(original_raw)!=ORIGINAL_COVERAGE:fail('PILOT_CLEANUP_ORIGINAL_COVERAGE_REQUIRED')
    write(operation/'original-b4c-coverage.json',original_raw);json_write(operation/'state-before.json',before)

def main():
    assert os.name=='posix' and os.getuid()==os.getgid()==10001 and not sys.flags.optimize;os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--owner-request',required=True);p.add_argument('--authorization-id')
    p.add_argument('--qualification',required=True);p.add_argument('--qualification-sha256',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--plan-file');p.add_argument('--plan-sha256');p.add_argument('--original-coverage')
    a=p.parse_args()
    validate_mode(a.apply,a.plan_file,a.plan_sha256)
    s=Settings.load();store=Store(s.db_path);retention=PilotPublishedRetentionV2(s,store)
    proof,qref=qualify(s.root,file=Path(a.qualification),checksum=a.qualification_sha256);request=owner_request(a.owner_request)
    control=safe(s.root/'data/pilot-published-cleanup-v2');control.mkdir(mode=0o700,exist_ok=True)
    if a.apply:
        plan_file=safe(Path(a.plan_file))
        if plan_file.name!='plan.json' or plan_file.parent.parent!=control or str(uuid.UUID(plan_file.parent.name))!=plan_file.parent.name:fail('PILOT_CLEANUP_EXACT_PLAN_REQUIRED')
        raw=read(plan_file);plan=json.loads(raw)
        if sha(raw)!=a.plan_sha256 or plan['job_id']!=JOB or plan['scope']!=PUBLISHED:fail('PILOT_CLEANUP_EXACT_PLAN_REQUIRED')
        auth=retention._authorization(plan['authorization_id'])
        if any(auth[k]!=v for k,v in request.items()):fail('PILOT_CLEANUP_REAL_OWNER_REQUEST_REQUIRED')
        if sha(read(plan_file.parent/'original-b4c-coverage.json',65536))!=ORIGINAL_COVERAGE:fail('PILOT_CLEANUP_ORIGINAL_COVERAGE_REQUIRED')
        samples=retained_samples(s.root,store.get(JOB)['payload'])
        with resource_guard(s.root/'data/locks/moderation'):
            before=snapshot(retention);result=retention.apply(plan);after=snapshot(retention)
        if before['intents']!=after['intents'] or before['unknown_result']!=after['unknown_result'] or before['state'].get('test_retention_policy')!=after['state'].get('test_retention_policy'):fail('PILOT_CLEANUP_ORIGINAL_HISTORY_CHANGED')
        json_write(plan_file.parent/'applied.json',{'result':result,'retained_samples':samples,'qualification':qref,'remote_deletion':False})
        print(encode({'state':'exact_three_files_applied','receipt':str(plan_file.parent/'applied.json'),'result':result}));return
    with resource_guard(s.root/'data/locks/video',s.root/'data/locks/moderation'):
        before=snapshot(retention)
        auth=retention._authorization(a.authorization_id) if a.authorization_id else retention.authorize(RETENTION_PRINCIPAL,scope=PUBLISHED,**request)
        if any(auth[k]!=v for k,v in request.items()) or auth['scope']!=PUBLISHED:fail('PILOT_CLEANUP_REAL_OWNER_REQUEST_REQUIRED')
        operation=control/str(uuid.uuid4());operation.mkdir(mode=0o700)
        template={'version':'video-source-retention-release-v2','authorization_id':auth['authorization_id'],'scope':PUBLISHED,
            'source_files':proof['source_files'],'test_attestation':{'file':qref['file'],'sha256':qref['sha256']}}
        json_write(operation/'root-release-template.json',template)
        try:retention._release({'job_id':JOB,'scope':PUBLISHED,'authorization_id':auth['authorization_id'],'proof':{'pipeline_state':'replay_highest_video_web_v2'}})
        except HubError as exc:
            if exc.code!='VIDEO_RETENTION_RELEASE_REQUIRED':raise
            print(encode({'state':'authorization_recorded_root_release_required','authorization_id':auth['authorization_id'],
                'release_template':str(operation/'root-release-template.json'),'files_deleted':0,'native_requests':0}));return
        preserve_before(s.root,before,operation,a.original_coverage)
        samples=retained_samples(s.root,before['binding'])
        plan=retention.plan(JOB,authorization_id=auth['authorization_id'],refresh_public_observation=fresh_callback(s,store,retention,operation))
        json_write(operation/'plan.json',plan);json_write(operation/'retained-samples.json',samples)
        print(encode({'state':'reviewable_plan_only','authorization_id':auth['authorization_id'],'plan_file':str(operation/'plan.json'),
            'plan_sha256':sha(read(operation/'plan.json')),'candidates':plan['candidates'],'files_deleted':0,'qualification':qref}))
if __name__=='__main__':main()
