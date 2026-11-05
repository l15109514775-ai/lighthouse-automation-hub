"""Resume one already-uploaded pilot after a proven repair; never retry/reset."""
from pathlib import Path
import argparse,hashlib,json,os,types,uuid

# Exact separately reviewed host applier; no Worker runtime source is added.
APPLIER_SHA='fbc2017b4dff42e9b64c07325e597967ccdd28684468c180477252540657de9f'

APP=r'''
from pathlib import Path
import hashlib,json,os,re,socket,sys,threading
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.worker import Worker
from services.hub.replay_moderation_common import QueueView
def refuse(*args,**kwargs):raise RuntimeError('UPLOADED_REPAIR_NETWORK_OR_CONSUMPTION_FORBIDDEN')
socket.socket.connect=refuse;Store.claim=Store.recover=Store.enqueue=Store.finish=refuse
Worker.once=Worker.run=QueueView.claim=QueueView.recover=threading.Thread.start=refuse
from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb,STATE
from services.hub.replay_highest_coverage_v2 import upload_coverage,expected_binding
from services.hub.short_video_web import PRINCIPAL,_reference
from services.hub.youtube_download import digest
def row_sha(row):return hashlib.sha256((json.dumps(row,sort_keys=True,separators=(',',':'))+'\n').encode()).hexdigest()
def resume_existing(settings,store,request,*,apply=False):
 assert type(apply) is bool and set(request)=={'job_id','before_row_sha256','repair_reference','intents_sha256'}
 reference=request['repair_reference']
 assert set(reference)=={'deployment_report','sha256','repair_attestation'} and Path(reference['deployment_report']).is_absolute()
 assert Path(reference['deployment_report']).name=='deployment-report.json' and re.fullmatch('[a-f0-9]{64}',reference['sha256'])
 qualification=reference['repair_attestation']
 assert set(qualification)=={'file','sha256','tests'} and qualification['tests']==68
 assert Path(qualification['file']).is_absolute() and Path(qualification['file']).name=='test-attestation.json' and re.fullmatch('[a-f0-9]{64}',qualification['sha256'])
 service=HighestReplayVideoWeb(settings,store);service._operator(PRINCIPAL)
 job,binding,state=service.snapshot(request['job_id'],write=False)
 assert job['job_id']=='594aed06-3aa1-45f4-90f0-1db49a602395'
 assert row_sha(job)==request['before_row_sha256'] and job['state']=='awaiting_review' and job['attempts']==2
 assert job['error_code'] in ('REPLAY_BROWSER_METADATA_PREPARATION_FAILED','METADATA_NATIVE_CONTROL_AMBIGUOUS','METADATA_CATEGORY_CHANGED')
 assert state['state']=='uploaded' and job['lease_owner'] is None and job['lease_until'] is None and job['external_publish_id'] is None
 assert job['source_id']==binding['source_id']=='9T5tn1WGlJg' and binding['download_job_id']=='d28685f1-03fc-4a12-9d85-5dbf14107f82'
 assert state['public_submission_approved'] is False and state['human_review'] is False and 'owner_acceptance' not in state
 assert job['result']['publish_performed'] is False and not state.get('result_observation') and not state.get('machine_acceptance')
 assert not store.query("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?",(job['source_id'],))
 policy=service._validate_snapshot_binding(job,binding,verify_artifact_hash=False)
 service._require_workflow_launch(policy)
 authorization=state.get('automation_authorization',{})
 assert authorization.get('version')==service.WORKFLOW_AUTHORIZATION_VERSION
 assert authorization.get('binding_sha256')==digest(binding) and authorization.get('workflow_authorization_reference')==policy['workflow_authorization_reference']
 assert authorization.get('human_review') is False and authorization.get('per_artifact_owner_acceptance') is False
 assert all(_reference(authorization.get(key)) for key in ('actual_owner_workflow_response','owner_response_reference'))
 folder=settings.root/'media'/job['job_id']/'replay-native-intents'
 assert folder.resolve()==folder and not any(p.is_symlink() for p in (folder,*folder.parents))
 assert {p.name for p in folder.iterdir()}=={'video-selection.json'}
 file=service._file(folder/'video-selection.json',job['job_id']);raw=file.read_bytes();assert 0<len(raw)<=65536
 intent_ref={'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
 assert request['intents_sha256']=={'video-selection.json':intent_ref['sha256']}
 intent=json.loads(raw);expected=expected_binding(job,binding,state)
 assert intent['version']=='replay-native-one-use-intent-v1' and intent['kind']=='video-selection'
 assert all(intent[key]==value for key,value in expected.items())
 observation=state['upload_observation']
 assert observation['attempt_id']==state['attempt_id'] and observation['site_uid']==binding['site_uid']
 assert observation['artifact_sha256']==binding['artifact_sha256'] and observation['file_bytes']==binding['file_bytes']
 assert observation['same_attempt_observed'] is True and observation['server_file_hash_verified'] is False
 coverage=upload_coverage(settings,job,binding,state,observation['upload_coverage'],fresh=False)
 assert coverage==state['upload_coverage']
 causal=digest({**expected,'local_causal_intent':intent_ref,'native_upload_complete':True,'upload_coverage':observation['upload_coverage']})
 assert causal==observation['upload_reference_sha256']
 report={'version':'uploaded-repair-resume-app-v1','job_id':job['job_id'],'state':'verified_plan_only','resumed':False,
  'before_row_sha256':row_sha(job),'attempts_before':job['attempts'],'attempt_id':state['attempt_id'],
  'repair_reference':request['repair_reference'],'new_authorization':False,'files_selected':0,'public_submissions':0,
  'fresh_upload_observation_claimed':False,'full_machine_qa_required_on_worker_resume':True}
 if not apply:return report
 store.audit(PRINCIPAL,'replay_uploaded_repair_resume_requested',job['job_id'],encode(request))
 service._validate_snapshot_binding(job,binding,verify_artifact_hash=False)
 service._update(PRINCIPAL,job,state,job_state='pending',stage='uploaded_repair_resume')
 after=store.get(job['job_id'])
 assert after['state']=='pending' and after['stage']=='uploaded_repair_resume' and after['error_code'] is None
 assert after['payload']==job['payload'] and after['result']==job['result'] and after['attempts']==job['attempts']
 assert all(after[key]==value for key,value in job.items() if key not in ('state','stage','error_code','updated_at'))
 store.audit(PRINCIPAL,'replay_uploaded_repair_resumed',job['job_id'],encode({'repair_reference':request['repair_reference'],
  'before_row_sha256':row_sha(job),'after_row_sha256':row_sha(after),'attempts_reset':False,'upload_attempt_unchanged':True}))
 return {**report,'state':'pending_same_uploaded_attempt','resumed':True,'after_row_sha256':row_sha(after)}
'''

def load_applier(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert len(raw)<=500000 and hashlib.sha256(raw).hexdigest()==APPLIER_SHA
    module=types.ModuleType('pinned_native_wait_applier');module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--applier-file',required=True);p.add_argument('--installer-file',required=True);p.add_argument('--deployer-file',required=True)
    p.add_argument('--cache-operator-file',required=True);p.add_argument('--deployment-report',required=True);p.add_argument('--deployment-sha256',required=True)
    p.add_argument('--resume',action='store_true');args=p.parse_args();assert os.name=='posix' and os.geteuid()==0
    a=load_applier(args.applier_file);operator=a.load_operator(args.cache_operator_file);m=operator.load(args.installer_file);d=m.helper(args.deployer_file)
    file=d.safe(Path(args.deployment_report));assert file.name=='deployment-report.json' and file.parent.parent==d.EV
    assert file.parent.name.startswith('replay-native-wait-v1-deploy-')
    suffix=file.parent.name[len('replay-native-wait-v1-deploy-'):];assert str(uuid.UUID(suffix))==suffix
    raw=d.read(file,500000);assert a.sha(raw)==args.deployment_sha256
    deployed=json.loads(raw);assert deployed['version']=='replay-native-wait-v1-deployment' and deployed['state']=='active' and deployed['activated'] is True
    assert deployed['production_changes']==a.PRODUCTION and deployed['target_job_id']==a.PILOT and deployed['job_row_unchanged'] is True
    references=deployed['references'];legacy,_=d.gate(Path(references['legacy']['file']),references['legacy']['sha256'])
    mixed,_=operator.mixed_gate(Path(references['current_mix']['file']),references['current_mix']['sha256'],m,d)
    repair,_=a.qualification(Path(references['repair']['file']),references['repair']['sha256'],m,d)
    a.validate_source_change(mixed['manifest']['candidate_runtime_source_sha256'],repair['manifest']['runtime_source_sha256'])
    m.protected(d.ROOT/'compose.yaml');before=d.compose_config();current=d.inspect(d.container_id('video-worker'))
    assert current['Id']==deployed['video_container_id']
    target=d.INSTALL/'deltas'/a.RELEASE;a.validate_installed(d,m,operator,before,current,target,repair['manifest']['runtime_source_sha256'])
    import fcntl
    locks=[]
    try:
        for role in ('video','moderation'):
            fd=m.role_lock(d,role)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:os.close(fd);raise RuntimeError('UPLOADED_REPAIR_ACTIVE_RESOURCE_NO_RESUME') from None
            locks.append(fd)
        observed=a.observation(d);a.job_payload_safe(observed['row'],observed['row']['result'],observed['publications'],set(observed['intents']))
        assert observed['video_processing']==0 and observed['row_sha256']==deployed['job_row_sha256'] and observed['intents']==deployed['intents_sha256']
        assert all(a.sha(d.read(Path(path),1048576))==digest for path,digest in deployed['configuration_sha256'].items())
        request={'job_id':a.PILOT,'before_row_sha256':observed['row_sha256'],'intents_sha256':observed['intents'],
            'repair_reference':{'deployment_report':str(file),'sha256':args.deployment_sha256,'repair_attestation':references['repair']}}
        program=APP+"\nassert os.getuid()==os.getgid()==10001\ns=Settings.load();db=Store(s.root/'data/hub.sqlite3')\nrequest=json.loads(sys.argv[1])\nprint(json.dumps(resume_existing(s,db,request,apply=sys.argv[2]=='resume')))\n"
        result=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',program,d.wire(request).decode(),
            'resume' if args.resume else 'plan'],timeout=90))
        assert result['resumed'] is args.resume and result['attempts_before']==2
        evidence=d.EV/('uploaded-repair-resume-v1-'+str(uuid.uuid4()));d.private_directory(evidence)
        d.atomic(evidence/'result.json',d.wire(result));print(json.dumps({**result,'evidence':str(evidence)}))
    finally:
        for fd in reversed(locks):os.close(fd)

if __name__=='__main__':main()
