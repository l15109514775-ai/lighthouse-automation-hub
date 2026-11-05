"""Exact remaining Highest master; historical public confirmation, zero HTTP.

Use the separately qualified current45 one-shot, never the live V5 worker.
Default creates a new one-file plan after one real complete SHA read. Apply
requires that saved plan's exact bytes and unchanged Linux inode/ctime. Neither
mode opens a browser or changes a publication, intent, source receipt or hold.
"""
from pathlib import Path
import argparse,hashlib,json,math,os,sqlite3,stat,time,types,uuid

from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.archive_source_retention_v2 import _inspect
from services.hub.pilot_published_retention_v2 import PilotPublishedRetentionV2,JOB,DOWNLOAD,SOURCE,BV,ATTEMPT
from services.hub.video_source_retention_v2 import PUBLISHED

VERSION='published-pilot-master-only-cleanup-v1'
ROOT=Path('/opt/lighthouse-hub')
AUTH='1fb26fc9-0324-4f13-9c30-64a767fbb012'
AUTH_SHA='8869d3ad9f68c317030ebbfdf5211075a624563e0adf9f653317a03747910eb9'
PREVIOUS=ROOT/'data/pilot-published-cleanup-v2/fee54175-6055-4b2a-9ba8-4186b91f48ba/plan.json'
PREVIOUS_SHA='b27f1559f47031132f548bb56fc0697bd9e3abf73095fc764729b74070ee1123'
MASTER=ROOT/'media'/DOWNLOAD/'youtube-highest-v2/original.mp4'
BYTES=10942323813
MASTER_SHA='b472d9dbc516973dbff246d2f7ae68a3eb83e8023e35bf24437a734f8056047b'
EVIDENCE_SHA='bb2d81e00f7a749a56a6e7c1678e1be46c3c7c4d9475a57fc71c2e4821b98f7c'
SUBMIT_SHA='2ada78df3cb3748eb3936f70a1c9fa767bae75b1c5d94ffb3e4fc3bc5b050521'
LATER_FILE=Path('/run/pilot-later-native-core-v1.py')
LATER_SHA='aeed27475855467d53fc65393ad40402984f1dfe28ea6e89a562aa46c2dd2d65'
HELPER_FILE=Path('/run/cleanup-pilot.py')
HELPER_SHA='abbeec1f746ef3522ade5520a9c72338fa7a56daec68a20f2bbb896cf40ee812'
CONTROL=ROOT/'data/pilot-published-master-cleanup-v1'
PRINCIPAL='operator:pilot-master-retention-v1'

def fail(code):raise HubError(code,review=True)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def digest(value):return sha(encode(value).encode())

def read(path,maximum=1_000_000):
    # Only bounded evidence/helper JSON or source; never used for the master.
    path=Path(path)
    if path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):fail('PILOT_MASTER_UNSAFE_PATH')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 0<before.st_size<=maximum:fail('PILOT_MASTER_FILE_BOUND')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno());current=path.stat()
    keys=('st_dev','st_ino','st_size','st_mtime_ns')
    if (len(raw)!=before.st_size or before.st_ctime_ns!=after.st_ctime_ns or
            any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(current,k) for k in keys) or
            os.name=='posix' and before.st_ctime_ns!=current.st_ctime_ns):fail('PILOT_MASTER_FILE_CHANGED')
    return raw

def load(path,checksum,name):
    raw=read(path)
    if sha(raw)!=checksum:fail('PILOT_MASTER_HELPER_SOURCE_CHANGED')
    mod=types.ModuleType(name);mod.__file__=str(path);mod.__package__='services.hub'
    exec(compile(raw,str(path),'exec'),mod.__dict__);return mod

def historical_confirmation(ret,later):
    job,binding,state,source,pub=ret._context(JOB)
    evidence=state.get('result_reconciliation',{})
    if (digest(evidence)!=EVIDENCE_SHA or evidence.get('version')!=later.VERSION or
            evidence.get('origin')!=later.ORIGIN or evidence.get('binding_sha256')!=digest(binding) or
            any(evidence.get(k) is not False for k in ('same_submit_attempt_observed','human_review','publication_authorization_inferred'))):
        fail('PILOT_MASTER_HISTORICAL_CONFIRMATION_REQUIRED')
    audits=[]
    for row in ret.store.query('SELECT at,principal,outcome FROM audit WHERE action=? AND target=? ORDER BY at',(later.VERSION,JOB)):
        try:value=json.loads(row['outcome'])
        except (ValueError,TypeError):continue
        at=row['at']
        if (row['principal']=='operator:ssh-sudo' and type(at) in (int,float) and math.isfinite(at) and 0<=at<=time.time() and
                value=={'origin':later.ORIGIN,'evidence_sha256':EVIDENCE_SHA,'same_submit_attempt_observed':False}):audits.append(at)
    if len(audits)!=1:fail('PILOT_MASTER_ORIGINAL_ACCEPTANCE_AUDIT_REQUIRED')
    refs={k:evidence[k] for k in ('public_observation','screenshot','manager_observation','manager_before_submit',
        'original_public_submit','original_unknown_result','original_machine_proof')}
    raw={k:later.read_reference(ret.root/'media'/JOB,v,maximum=20_000_000) for k,v in refs.items()}
    public=json.loads(raw['public_observation']);duration=source['result']['youtube_download_v2']['binding']['duration']
    validated=later.validate_public(public,{**binding,'job_id':JOB},ATTEMPT,duration,now=audits[0])
    posted=later.validate_manager(json.loads(raw['manager_observation']),{**binding,'job_id':JOB},BV)
    intent=json.loads(raw['original_public_submit'])
    if (validated['external_id']!=BV or validated['url']!=pub['external_url'] or
            not later.when(intent['created_at'])<=posted<=later.when(validated['observed_at'])+60 or
            json.loads(raw['original_unknown_result'])!={'state':'unknown','submitted':None,'reconcile_required':True} or
            sha(raw['original_public_submit'])!=SUBMIT_SHA):
        fail('PILOT_MASTER_HISTORICAL_CONFIRMATION_REQUIRED')
    from services.hub.replay_highest_coverage_v2 import upload_coverage
    uploaded=upload_coverage(ret.settings,job,binding,state,state['upload_observation']['upload_coverage'],fresh=False)
    if uploaded!=state['upload_coverage']:fail('PILOT_MASTER_UPLOAD_PROOF_CHANGED')
    return {'historical_confirmation':True,'current_access_requeried':False,'acceptance_audit_at':audits[0],
        'evidence_sha256':EVIDENCE_SHA,'original_refs':refs,'external_id':BV,'duration_seconds':duration,
        'platform_requests':0,'public_observed_at':validated['observed_at']}

def context(ret):
    job,binding,state,source,pub=ret._context(JOB)
    if any(ret.store.query("SELECT 1 FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)")):
        fail('PILOT_MASTER_VIDEO_WORKER_ACTIVE')
    auth=ret._authorization(AUTH)
    if digest(auth)!=AUTH_SHA or auth['scope']!=PUBLISHED or auth['artifact'] is not None:fail('PILOT_MASTER_OWNER_INSTRUCTION_CHANGED')
    marker=source['result'].get('redundant_source_tracks_retention_pilot_v5',{})
    if (marker.get('version')!='pilot-redundant-raw-only-receipt-v5' or marker.get('raw_files_deleted')!=2 or
            marker.get('raw_logical_bytes_deleted')!=10921804989 or marker.get('master_retained')!=
            {'file':str(MASTER),'bytes':BYTES,'sha256':MASTER_SHA}):fail('PILOT_MASTER_RAW_CUSTODY_REQUIRED')
    if any(MASTER.with_name(n).exists() for n in ('stream-299.mp4','stream-140.m4a')):fail('PILOT_MASTER_RAW_CUSTODY_REQUIRED')
    return {'job':digest(job),'source':digest(source),'publication':digest(pub),'authorization':digest(auth),
        'updated_at':{JOB:job['updated_at'],DOWNLOAD:source['updated_at']}}

def prior_proof(ret):
    raw=read(PREVIOUS)
    if sha(raw)!=PREVIOUS_SHA:fail('PILOT_MASTER_ORIGINAL_PROOF_CHANGED')
    value=json.loads(raw);master=value['candidate']
    job,binding,state,source,pub=ret._context(JOB);download=source['result']['youtube_download_v2'];asset=download['source_asset']
    if (value['job_id']!=JOB or value['scope']!=PUBLISHED or value['authorization_id']!=AUTH or value['authorization_sha256']!=AUTH_SHA or
            master['file']!=str(MASTER) or master['bytes']!=BYTES or master['sha256']!=MASTER_SHA or
            asset['file']!=str(MASTER) or asset['bytes']!=BYTES or asset['sha256']!=MASTER_SHA or
            download['receipt'].get('source_to_merged_packet_coverage_verified') is not True or
            asset['packet_evidence']!=value['proof']['basis']['local_packet_evidence']):fail('PILOT_MASTER_ORIGINAL_PROOF_CHANGED')
    return master,{'file':str(PREVIOUS),'sha256':PREVIOUS_SHA,'packet_proof_sha256':digest(asset['packet_evidence']),
        'current_master_full_sha_required':True,'packet_scan_reused_only_after_exact_current_sha':True}

def write(path,value):
    raw=encode(value).encode();fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
    if os.name=='posix':
        fd=os.open(path.parent,os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    return sha(raw)

def create_plan(ret,later):
    before=context(ret);historical=historical_confirmation(ret,later);expected,proof=prior_proof(ret)
    before_stat=MASTER.lstat();actual=_inspect(ret.root,MASTER,14_000_000_000);after_stat=MASTER.lstat()
    if actual!=expected or any(getattr(before_stat,k)!=getattr(after_stat,k)
            for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):fail('PILOT_MASTER_CHANGED')
    ctime=before_stat.st_ctime_ns
    if context(ret)!=before:fail('PILOT_MASTER_CONTEXT_CHANGED')
    ret._release({'job_id':JOB,'scope':PUBLISHED,'authorization_id':AUTH,'proof':{'pipeline_state':'replay_highest_video_web_v2'}})
    return {'version':VERSION,'plan_id':str(uuid.uuid4()),'job_id':JOB,'download_job_id':DOWNLOAD,'source_id':SOURCE,
        'authorization_id':AUTH,'authorization_sha256':AUTH_SHA,'candidate':actual,'ctime_ns':ctime,'snapshot':before,
        'historical_public_confirmation':historical,'local_packet_basis':proof,'operator_sha256':sha(read(Path(__file__).resolve())),
        'created_at':time.time(),'platform_requests':0,'original_container_recoverability_claimed':False,
        'raw_files_already_deleted':2,'private_samples_retained':True,'candidate_count':1,'publication_changed':False}

def apply_plan(ret,later,plan,plan_sha,index):
    if plan.get('version')!=VERSION or plan.get('candidate_count')!=1 or plan.get('job_id')!=JOB or plan.get('download_job_id')!=DOWNLOAD:
        fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
    if plan.get('operator_sha256')!=sha(read(Path(__file__).resolve())):fail('PILOT_MASTER_OPERATOR_CHANGED')
    index.execute('PRAGMA synchronous=FULL');index.execute('CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,plan_sha TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL)')
    row=index.execute('SELECT plan_sha,state,record FROM operations WHERE id=?',(plan['plan_id'],)).fetchone()
    if row and row[0]!=plan_sha:fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
    if row and row[1]=='completed':return json.loads(row[2])
    receipt=None
    if row:
        body=json.loads(row[2])
        if body.get('plan')!=plan or row[1]!='deleting':fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
        current=ret.store.get(JOB);source=ret.store.get(DOWNLOAD)
        a=current['result'].get('local_original_retention_v2');b=source['result'].get('local_original_retention_v2')
        if a and a==b and a.get('plan_id')==plan['plan_id'] and a.get('file_sha256')==MASTER_SHA and not MASTER.exists():
            if (a.get('version')!=VERSION or a.get('file')!=str(MASTER) or a.get('file_bytes')!=BYTES or
                    a.get('job_id')!=JOB or a.get('download_job_id')!=DOWNLOAD or a.get('authorization_id')!=AUTH):fail('PILOT_MASTER_RECOVERY_CHANGED')
            observed=context(ret)
            if any(observed[k]!=plan['snapshot'][k] for k in ('publication','authorization')):fail('PILOT_MASTER_RECOVERY_CHANGED')
            for value,key in ((current,'job'),(source,'source')):
                original=json.loads(encode(value));original['result'].pop('local_original_retention_v2');original['updated_at']=plan['snapshot']['updated_at'][value['job_id']]
                if digest(original)!=plan['snapshot'][key]:fail('PILOT_MASTER_RECOVERY_CHANGED')
            receipt=a
    if receipt is None:
        if context(ret)!=plan['snapshot'] or historical_confirmation(ret,later)!=plan['historical_public_confirmation']:fail('PILOT_MASTER_CONTEXT_CHANGED')
        expected,proof=prior_proof(ret)
        if plan['candidate']!=expected or plan['local_packet_basis']!=proof:fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
        if not MASTER.exists() and not row:fail('PILOT_MASTER_UNEXPECTED_MISSING')
        # Full SHA was verified by this plan; Linux ctime/inode and root-protected
        # saved-plan hash fence every later write without a second 11GB scan.
        with ret.store.transaction() as db:
            if context(ret)!=plan['snapshot']:fail('PILOT_MASTER_CONTEXT_CHANGED')
            ret._unshared(db,[str(MASTER)],JOB,DOWNLOAD)
            index.execute('INSERT OR IGNORE INTO operations VALUES(?,?,?,?)',(plan['plan_id'],plan_sha,'deleting',encode({'plan':plan})));index.commit()
            if MASTER.exists():ret._stat_unlink(plan['candidate'],plan['ctime_ns'])
            receipt={'version':VERSION,'plan_id':plan['plan_id'],'job_id':JOB,'download_job_id':DOWNLOAD,'source_id':SOURCE,
                'scope':PUBLISHED,'file':str(MASTER),'file_sha256':MASTER_SHA,'file_bytes':BYTES,'local_files_deleted':1,
                'total_logical_bytes_deleted':BYTES,'historical_confirmation':True,'current_access_requeried':False,'platform_requests':0,
                'publication_records_preserved':True,'source_id_idempotency_records_preserved':True,'remote_video_deleted':False,
                'evidence_files_deleted':False,'private_samples_retained':True,'authorization_id':AUTH,'deleted_at':time.time()}
            for ident in (JOB,DOWNLOAD):
                value=ret.store.get(ident);db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',
                    (encode({**value['result'],'local_original_retention_v2':receipt}),time.time(),ident))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(time.time(),PRINCIPAL,VERSION,JOB,encode(receipt)))
    index.execute('UPDATE operations SET state=?,record=? WHERE id=?',('completed',encode(receipt),plan['plan_id']));index.commit();return receipt

def main():
    assert os.name=='posix' and os.getuid()==os.getgid()==10001 and not __import__('sys').flags.optimize;os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qualification',required=True);p.add_argument('--qualification-sha256',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--plan');p.add_argument('--plan-sha256');a=p.parse_args()
    if a.apply!=bool(a.plan and a.plan_sha256) or not a.apply and (a.plan or a.plan_sha256):fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
    helper=load(HELPER_FILE,HELPER_SHA,'qualified_pilot_cleanup');helper.qualify(ROOT,file=Path(a.qualification),checksum=a.qualification_sha256)
    later=load(LATER_FILE,LATER_SHA,'services.hub.qualified_later_public_v1');settings=Settings.load();ret=PilotPublishedRetentionV2(settings,Store(settings.db_path))
    with resource_guard(ROOT/'data/locks/video',ROOT/'data/locks/moderation',ROOT/'data/locks/heavy'):
        from services.hub.archive_source_retention_v2 import _path
        _path(ROOT,CONTROL);CONTROL.mkdir(mode=0o700,exist_ok=True)
        info=CONTROL.stat()
        if info.st_uid!=10001 or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700:fail('PILOT_MASTER_PRIVATE_CONTROL_REQUIRED')
        if not a.apply:
            plan=create_plan(ret,later);file=CONTROL/(plan['plan_id']+'.json');checksum=write(file,plan)
            print(encode({'state':'reviewable_master_only_plan','plan_file':str(file),'plan_sha256':checksum,'candidate':plan['candidate'],'files_deleted':0,'platform_requests':0}));return
        file=_path(ROOT,Path(a.plan))
        if file.parent!=CONTROL:fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
        raw=read(file);plan=json.loads(raw)
        if sha(raw)!=a.plan_sha256 or file.name!=str(uuid.UUID(plan['plan_id']))+'.json':fail('PILOT_MASTER_EXACT_PLAN_REQUIRED')
        ret._release({'job_id':JOB,'scope':PUBLISHED,'authorization_id':AUTH,'proof':{'pipeline_state':'replay_highest_video_web_v2'}})
        index_path=_path(ROOT,CONTROL/'intents.sqlite3')
        with sqlite3.connect(index_path) as index:
            index.execute('PRAGMA journal_mode=WAL');result=apply_plan(ret,later,plan,a.plan_sha256,index)
        print(encode({'state':'master_only_deleted','receipt':result}))
if __name__=='__main__':main()
