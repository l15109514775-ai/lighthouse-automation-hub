"""Default-plan recording of later public evidence; never starts a browser."""
from pathlib import Path
import argparse, hashlib, json, os, types

NAV_APPLIER='118189718b4b41e0728db3eb383c93e1c1fa2dc99e585803d9c571d9adbc398e'
JOB='594aed06-3aa1-45f4-90f0-1db49a602395'
INTENT='2ada78df3cb3748eb3936f70a1c9fa767bae75b1c5d94ffb3e4fc3bc5b050521'

def sha(raw):return hashlib.sha256(raw).hexdigest()

def load(file,expected,name):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    raw=file.read_bytes();assert 0<len(raw)<200000 and sha(raw)==expected
    module=types.ModuleType(name);module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module

APP=r'''
import hashlib,json,os,sys
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.locks import resource_guard
from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb
from services.hub.short_video_web import PRINCIPAL
assert os.getuid()==os.getgid()==10001 and not sys.flags.optimize
request=json.loads(sys.argv[1]);raw=bytes.fromhex(request.pop('core_hex'))
assert hashlib.sha256(raw).hexdigest()==request['core_sha256']
scope={'__name__':'services.hub._later_native_candidate','__package__':'services.hub'}
exec(compile(raw,'<pinned-later-native-core>','exec'),scope)
s=Settings.load();db=Store(s.db_path);service=HighestReplayVideoWeb(s,db);service._operator(PRINCIPAL)
locks=s.root/'data/locks'
with resource_guard(*(locks/name for name in ('video','moderation','bilibili-video-profile','heavy'))):
 for file,digest in request['pins'].items():
  assert hashlib.sha256((s.root/file).read_bytes()).hexdigest()==digest
 job=db.get(request['job_id']);assert job['attempts']==3
 assert job['result'][service.STATE]['attempt_id']=='150c1074-4aed-4f5d-b87a-83cf2840af24'
 intent=s.root/'media'/job['job_id']/'replay-native-intents/public-submit.json'
 assert hashlib.sha256(intent.read_bytes()).hexdigest()==request['intent_sha256']
 result=scope['record_later_native_result'](service,PRINCIPAL,job['job_id'],
  public_reference=request['public'],screenshot_reference=request['screenshot'],manager_reference=request['manager'],
  actual_operator_reference=request['operator_reference'],apply=sys.argv[2]=='record')
 assert hashlib.sha256(intent.read_bytes()).hexdigest()==request['intent_sha256']
 assert db.get(job['job_id'])['attempts']==3
 for file,digest in request['pins'].items():assert hashlib.sha256((s.root/file).read_bytes()).hexdigest()==digest
 print(json.dumps(result))
'''

def main():
    assert os.name=='posix' and os.geteuid()==0 and not __import__('sys').flags.optimize
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('core-file','core-sha256','navigation-applier-file','helper-file','attestation','archive','deployment-report',
                 'public-file','public-sha256','screenshot-file','screenshot-sha256','manager-file','manager-sha256','operator-reference'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--record',action='store_true');args=p.parse_args()
    a=load(args.navigation_applier_file,NAV_APPLIER,'pinned_navigation_applier');d=a.load_helper(args.helper_file)
    manifest,_,qualification=a.qualification(d,args.attestation,args.archive)
    current=d.inspect(d.cid('video-worker'));assert current['State']['Running'] and current['Image']==a.IMAGE
    assert current['Config']['User']=='10001:10001';a.sources(d,current['Id'],manifest['runtime_source_sha256'])
    report=d.safe(Path(args.deployment_report));d.protected(report);deployed=json.loads(d.read(report,500000))
    assert deployed['version']=='saved-draft-navigation-single-source-v3' and deployed['state']=='active_verified_no_lookup_run'
    assert deployed['video_container_id']==current['Id'] and deployed['qualification']==qualification
    config=deployed['configuration_sha256'];assert all(sha(d.read(Path(f),2000000))==h for f,h in config.items())
    core=d.read(d.safe(Path(args.core_file)),200000);assert sha(core)==args.core_sha256
    def reference(file,expected,maximum):
        file=d.safe(Path(file));assert file.is_relative_to(d.ROOT/'media'/JOB)
        raw=d.read(file,maximum);assert sha(raw)==expected
        return {'file':str(file),'bytes':len(raw),'sha256':expected}
    request={'job_id':JOB,'intent_sha256':INTENT,'core_hex':core.hex(),'core_sha256':args.core_sha256,
        'public':reference(args.public_file,args.public_sha256,1000000),
        'screenshot':reference(args.screenshot_file,args.screenshot_sha256,20000000),
        'manager':reference(args.manager_file,args.manager_sha256,1000000),'operator_reference':args.operator_reference,
        'pins':{**manifest['runtime_source_sha256'],**{str(Path(f).relative_to(d.ROOT)):h for f,h in config.items()}}}
    others=a.other_cores(d)
    result=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',APP,
                             json.dumps(request,ensure_ascii=False),'record' if args.record else 'plan'],timeout=90))
    assert a.other_cores(d)==others and d.inspect(current['Id'])['State']['Running']
    assert all(sha(d.read(Path(f),2000000))==h for f,h in config.items())
    a.sources(d,current['Id'],manifest['runtime_source_sha256'])
    area=d.STATE if hasattr(d,'STATE') else Path('/var/lib/lighthouse-later-native-result-v1')
    # A new private receipt; no original deployment report is rewritten.
    if not area.exists():d.directory(area,0o700)
    import uuid
    output=area/('later-native-reconciliation-'+str(uuid.uuid4()));d.directory(output,0o700)
    result.update(core_sha256=args.core_sha256,qualification=qualification,current_container_id=current['Id'],
                  other_cores_unchanged=True,raw_environment_exported=False)
    d.atomic(output/'result.json',d.wire(result));print(json.dumps({**result,'receipt':str(output/'result.json')}))

if __name__=='__main__':main()
