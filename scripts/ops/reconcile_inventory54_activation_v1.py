"""Read-only runtime reconciliation after the successful cutover's Env-order assertion."""
import hashlib,json,subprocess,time,urllib.request,re
from pathlib import Path
from datetime import datetime
from copy import deepcopy
import importlib.util
ROOT=Path('/opt/lighthouse-hub');AREA=Path('/var/lib/lighthouse-replay-inventory54-v1/4cce5de8-19b7-4d56-9ad3-bf5bf0c4c285')
CID='c2e6401f6d2f422f495310770813a4ee64ca73235f7a55f1fd80aa0a9794b30b'
OLD='sha256:536447e514b8656f010e31c869327df5a03035c502893f22c8e8023bd8710d95'
IMAGE='sha256:ac38b4c2e349cb3d02288f5fdebd68af058d52e439c686881849f090b0508db6'
PROOF='/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/8cffefb2-c7fd-424c-97f5-aa73c22ebb23'
raw=Path('/home/lighthouse-ops/deploy_replay_parts_pilot_v5.py').read_bytes();assert hashlib.sha256(raw).hexdigest()=='e0e24d11d322f419db5a6092e1176b12d938b5577919c67977d28fe5d60813aa'
spec=importlib.util.spec_from_file_location('frozen_pilot','/home/lighthouse-ops/deploy_replay_parts_pilot_v5.py');d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
old_report=d.read(AREA/'report.json');report=json.loads(old_report);assert report['state']=='installed_restart_pending' and report['image']==IMAGE
before=json.loads(d.read(AREA/'compose.before.json'));current=json.loads(d.read(ROOT/'compose.yaml'));reverse=deepcopy(current);reverse['services']['video-worker']['image']=OLD
next(v for v in reverse['services']['video-worker']['volumes'] if v['target']=='/run/qualifications/retention-v5')['source']='/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/3f43e6d2-83a1-464a-948e-ef0c45f05423'
assert reverse==before
new=d.inspect('video-worker');assert new['Id']==CID and new['Image']==IMAGE and new['State']['Running'];service=current['services']['video-worker']
image=json.loads(d.run(['docker','image','inspect',OLD]).stdout)[0];expected=dict(v.split('=',1) for v in image['Config']['Env']);assert len(expected)==len(image['Config']['Env']);expected.update({k:str(v) for k,v in service['environment'].items()})
assert dict(v.split('=',1) for v in new['Config']['Env'])==expected and len(expected)==len(new['Config']['Env'])
assert new['Config']['Cmd']==service['command'] and new['Config']['Entrypoint']==service['entrypoint'] and new['Config']['User']==service['user'] and new['Config']['WorkingDir']==image['Config']['WorkingDir']
binds={v['Destination']:(v['Source'],v['RW']) for v in new['Mounts'] if v['Type']=='bind'}
assert binds=={v['target']:(v['source'],not v.get('read_only',False)) for v in service['volumes']}
host=new['HostConfig'];assert host['ReadonlyRootfs'] is True and host['Privileged'] is False and host['CapDrop']==['ALL']
def size(value):
 if type(value) is int:return value
 match=re.fullmatch(r'([0-9]+)([kmg]?)b?',str(value).lower());assert match
 return int(match[1])*{'':1,'k':1024,'m':1024**2,'g':1024**3}[match[2]]
assert host['Memory']==size(service['mem_limit']) and host['NanoCpus']==round(float(service['cpus'])*1000000000) and host['PidsLimit']==int(service['pids_limit']) and host['ShmSize']==size(service['shm_size'])
assert host['PortBindings']=={} and len(host['SecurityOpt'])==2 and 'no-new-privileges:true' in host['SecurityOpt']
profile=json.loads((ROOT/'config/browser-seccomp.json').read_bytes());seccomp=next(s.removeprefix('seccomp=') for s in host['SecurityOpt'] if s.startswith('seccomp='));assert json.loads(seccomp)==profile
assert host['Tmpfs']==dict(s.split(':',1) for s in service['tmpfs']) and host['RestartPolicy']['Name']==service['restart'] and host['Init'] is True
for name,ident in report['other_four'].items():assert d.identity(d.inspect(name))==ident
for name,value in d.FIXED_CONFIG.items():assert hashlib.sha256(d.read(ROOT/'config'/name)).hexdigest()==value
for name in d.NAMES:
 if name!='replay-parts-retention-v5-release.json':assert d.read(ROOT/'config'/name)==d.read(AREA/'config'/name)
check="import json,sqlite3;from services.hub.config import Settings;from services.hub.db import Store;from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5;s=Settings.load();ReplayPartsRetentionV5(s,Store(s.db_path))._release();c=sqlite3.connect(s.db_path.as_uri()+'?mode=ro',uri=True);print(json.dumps(dict(c.execute(\"SELECT name,at FROM heartbeats WHERE name IN ('worker:video-main-v5','worker:video-producer-v5','worker:video-part-moderation-v5','worker:video-retention-v5')\"))))"
first=json.loads(d.run(['docker','exec','--user','10001:10001',CID,'python','-c',check]).stdout);started=datetime.fromisoformat(new['State']['StartedAt'].replace('Z','+00:00')).timestamp()
assert set(first)==set(d.BEATS) and all(started<=v<=time.time()+1 and time.time()-v<90 for v in first.values());deadline=time.monotonic()+120
while True:
 time.sleep(2);beats=json.loads(d.run(['docker','exec','--user','10001:10001',CID,'python','-c',check]).stdout)
 if set(beats)==set(first) and all(beats[k]>first[k] for k in first):break
 assert time.monotonic()<deadline
with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as r:ready=json.loads(r.read(8192));assert r.status==200 and ready['status']=='ready' and ready['stale_services']==[]
d.save(AREA/'report.before-readonly-reconciliation.json',old_report)
report.update(state='video_inventory_fix_active',new_cid=CID,ready=ready,heartbeats=beats,heartbeat_baseline=first,other_services_unchanged=True,post_check_failure='Environment list order assertion; identical key/value map verified without restarting',reconciliation={'read_only_runtime_verification':True,'commands_replayed':False,'old_container_removed_by_compose':True,'old_runtime_snapshot_available':False,'comparison_basis':'immutable old-image and original compose configuration','resources_checked_against_original_compose_and_qualified_limits':True,'previous_report_sha256':hashlib.sha256(old_report).hexdigest(),'observed_at':time.time()})
d.save(AREA/'report.json',d.wire(report)+b'\n');print(json.dumps({'state':report['state'],'new_cid':CID,'report':str(AREA/'report.json'),'ready':ready}))
