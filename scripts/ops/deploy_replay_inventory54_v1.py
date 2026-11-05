"""Video-only qualified inventory fix and matching actual retention release."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import argparse,hashlib,json,os,subprocess,time,types,uuid
import urllib.request
ROOT=Path('/opt/lighthouse-hub');STATE=Path('/var/lib/lighthouse-replay-inventory54-v1')
CURRENT='971c6cc495b2836b400ea0fea22c1d72c7d0259c9e4ca7a076265d8fe1b11dc3'
OLD_IMAGE='sha256:536447e514b8656f010e31c869327df5a03035c502893f22c8e8023bd8710d95'
IMAGE='sha256:ac38b4c2e349cb3d02288f5fdebd68af058d52e439c686881849f090b0508db6'
DEPLOY_SHA='e0e24d11d322f419db5a6092e1176b12d938b5577919c67977d28fe5d60813aa'
DELTA_HELPER_SHA='3b3dd755a3af6750cc5b07f3d523d54eea12d2b6ffea656f56cfbe92e661516f'
PARENT=Path('/home/lighthouse-ops/lighthouse-evidence/source-candidate-c0c335eb-bdc4-460c-ae4e-dc42493c018a/test-result.json')
DELTA=Path('/home/lighthouse-ops/lighthouse-evidence/youtube-real-inventory54-3f83b16b-e15f-42d0-9cfa-b8d3012bf63e/test-result.json')
OWNER=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/owner-authorization-1fb26fc9.json')
OLD_PROOF=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/3f43e6d2-83a1-464a-948e-ef0c45f05423')
def load(path,digest,name):
 raw=Path(path).read_bytes();assert hashlib.sha256(raw).hexdigest()==digest
 mod=types.ModuleType(name);mod.__file__=str(Path(path).resolve());exec(compile(raw,str(path),'exec'),mod.__dict__);return mod
def candidate(before,proof):
 after=deepcopy(before);service=after['services']['video-worker'];assert service['image']==OLD_IMAGE
 service['image']=IMAGE;mounts=[v for v in service['volumes'] if v['target']=='/run/qualifications/retention-v5'];assert len(mounts)==1
 assert mounts[0]['source']==str(OLD_PROOF) and mounts[0]['read_only'] is True;mounts[0]['source']=str(proof)
 check=deepcopy(after);check['services']['video-worker']['image']=OLD_IMAGE
 next(v for v in check['services']['video-worker']['volumes'] if v['target']=='/run/qualifications/retention-v5')['source']=str(OLD_PROOF)
 assert check==before;return after
def main():
 p=argparse.ArgumentParser();p.add_argument('--deploy-helper',required=True);p.add_argument('--delta-helper',required=True);p.add_argument('--retention-helper',required=True)
 p.add_argument('--retention-proof',required=True);p.add_argument('--retention-sha256',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
 assert os.name=='posix' and os.geteuid()==0 and not __debug__ is False;os.umask(0o077)
 d=load(a.deploy_helper,DEPLOY_SHA,'frozen_pilot_deployer');q=load(a.delta_helper,DELTA_HELPER_SHA,'inventory54_qualification');m=q.load(a.retention_helper)
 praw=m.read(PARENT);draw=m.read(DELTA);assert m.sha(praw)==q.PARENT_SHA and m.sha(draw)==q.DELTA_SHA
 parent=json.loads(praw);delta=json.loads(draw);m.validate_parent(parent,m.read(Path(parent['junit_file'])))
 lineage=q.validate_delta(m,delta,m.read(Path(delta['junit_file'])),parent);assert parent['image']==OLD_IMAGE and delta['image']==IMAGE
 base=m.inspect_image(OLD_IMAGE);fresh=m.inspect_image(IMAGE);assert fresh['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers']
 pf=Path(a.retention_proof).parent;raw=m.read(Path(a.retention_proof),protected=True);assert m.sha(raw)==a.retention_sha256
 proof=json.loads(raw);assert pf.parent==m.BASE and str(uuid.UUID(pf.name))==pf.name and proof['host_file']==a.retention_proof
 assert proof['qualification_lineage']==lineage and proof['qualification_helper_sha256']==DELTA_HELPER_SHA and proof['original_qualification_helper_sha256']==q.HELPER_SHA
 assert proof['source_files']==m.source_files(delta['source_manifest']) and proof['image']==IMAGE and proof['qualified_parent_sha256']==q.DELTA_SHA
 assert proof['source_archive_sha256']==q.DELTA_ARCHIVE and proof['source_commit']==q.DELTA_COMMIT and proof['source_manifest_sha256']==lineage['composed_source_manifest_sha256']
 xml=m.read(pf/'results.xml',protected=True);assert m.sha(xml)==proof['junit_sha256'] and m.junit(xml,26,group=m.GROUP)==proof['test_groups']
 assert proof['version']==m.VERSION and proof['tests']==26 and proof['exit_code']==proof['failures']==proof['errors']==proof['skipped']==0 and proof['network']=='none'
 assert all(proof[k] is True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','mounted_sources_unchanged'))
 assert all(proof[k] is False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed','timed_out'))
 authorization=json.loads(m.read(OWNER,protected=True));release=m.release_template(authorization,proof,raw)
 old=d.inspect('video-worker');assert old['Id']==CURRENT and old['Image']==OLD_IMAGE and old['State']['Running']
 compose=d.read(ROOT/'compose.yaml');before=json.loads(compose);after=candidate(before,pf)
 peers={name:d.identity(d.inspect(name)) for name in d.PEERS};environment=before['services']['video-worker']['environment']
 configs={n:d.read(ROOT/'config'/n) for n in (*d.FIXED_CONFIG,*d.NAMES)}
 oldrelease=json.loads(configs[m.RELEASE]);assert oldrelease['authorization_id']==release['authorization_id'] and oldrelease['authorization_sha256']==release['authorization_sha256']
 assert {n for n in oldrelease['source_files'] if oldrelease['source_files'][n]!=release['source_files'][n]}=={q.SOURCE}
 m.root_directory(STATE);folder=m.root_directory(STATE/str(uuid.uuid4()));d.save(folder/'snapshot.sqlite3',d.host_snapshot())
 d.video_rows(folder/'snapshot.sqlite3');d.save(folder/'compose.before.json',compose,0o600);d.save(folder/'release.before.json',configs[m.RELEASE])
 cfg=m.root_directory(folder/'config');private={**configs,'hub.toml':configs['video-runtime-v5.toml'],m.RELEASE:m.wire(release)+b'\n'}
 seccomp=(ROOT/'config/browser-seccomp.json').read_bytes();assert m.sha(seccomp)==delta['source_manifest']['files']['config/browser-seccomp.json']['sha256'];private['browser-seccomp.json']=seccomp
 for name,value in private.items():d.save(cfg/name,value)
 d.save(folder/'browser-seccomp.json',seccomp)
 report={'version':'replay-inventory54-cutover-v1','state':'plan_only','image':IMAGE,'old_cid':CURRENT,'parent_276_sha256':q.PARENT_SHA,'delta54_sha256':q.DELTA_SHA,
  'retention26_sha256':a.retention_sha256,'lineage':lineage,'other_four':peers,'only_image_and_retention_proof_change':True,'queue_reset':False,'owner_window_touched':False}
 report['preflight']=d.preflight(IMAGE,folder,pf,environment,seccomp=folder/'browser-seccomp.json');d.save(folder/'report.json',d.wire(report)+b'\n')
 if not a.apply:print(json.dumps({'state':'verified_plan_only','report':str(folder/'report.json'),'preflight':report['preflight']}));return
 with d.idle_locks():
  assert d.inspect('video-worker')['Id']==CURRENT and d.read(ROOT/'compose.yaml')==compose and all(d.identity(d.inspect(n))==v for n,v in peers.items())
  assert all(d.read(ROOT/'config'/n)==v for n,v in configs.items())
  d.save(folder/'before-stop.sqlite3',d.host_snapshot());rows=d.video_rows(folder/'before-stop.sqlite3')
  d.run(d.command('stop','--timeout','45','video-worker'),timeout=60);assert not json.loads(d.run(['docker','inspect',CURRENT]).stdout)[0]['State']['Running']
  d.save(ROOT/'config'/m.RELEASE,m.wire(release)+b'\n');d.save(ROOT/'compose.yaml',d.wire(after)+b'\n')
  report['state']='installed_restart_pending';d.save(folder/'report.json',d.wire(report)+b'\n')
  d.save(folder/'after-install.sqlite3',d.host_snapshot());assert d.video_rows(folder/'after-install.sqlite3')==rows
 d.run(d.command('up','-d','--no-deps','--no-build','--pull','never','video-worker'),timeout=120);new=d.inspect('video-worker')
 assert new['Image']==IMAGE and new['State']['Running']
 assert sorted(new['Config']['Env'])==sorted(old['Config']['Env'])
 for key in ('Cmd','User','Entrypoint','WorkingDir'):assert new['Config'][key]==old['Config'][key]
 for key in old['HostConfig']:
  if key not in ('Binds','Mounts'):assert old['HostConfig'][key]==new['HostConfig'][key]
 mounts=deepcopy(new['Mounts']);entry=next(v for v in mounts if v['Destination']=='/run/qualifications/retention-v5');assert entry['Source']==str(pf) and entry['RW'] is False;entry['Source']=str(OLD_PROOF);assert mounts==old['Mounts']
 check="import json,sqlite3;from services.hub.config import Settings;from services.hub.db import Store;from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5;s=Settings.load();ReplayPartsRetentionV5(s,Store(s.db_path))._release();c=sqlite3.connect(s.db_path.as_uri()+'?mode=ro',uri=True);print(json.dumps(dict(c.execute(\"SELECT name,at FROM heartbeats WHERE name IN ('worker:video-main-v5','worker:video-producer-v5','worker:video-part-moderation-v5','worker:video-retention-v5')\"))))"
 started=datetime.fromisoformat(new['State']['StartedAt'].replace('Z','+00:00')).timestamp();deadline=time.monotonic()+180;first=None
 while True:
  beats=json.loads(d.run(['docker','exec','--user','10001:10001',new['Id'],'python','-c',check]).stdout)
  if set(beats)==set(d.BEATS) and all(v>=started for v in beats.values()):
   if first is not None and all(beats[k]>first[k] for k in beats):break
   if first is None:first=beats.copy()
  assert time.monotonic()<deadline;time.sleep(2)
 assert all(d.identity(d.inspect(n))==v for n,v in peers.items()) and all(d.read(ROOT/'config'/n)==v for n,v in configs.items() if n!=m.RELEASE)
 with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:ready=json.loads(response.read(8192));assert response.status==200 and ready['status']=='ready' and ready['stale_services']==[]
 report.update(state='video_inventory_fix_active',new_cid=new['Id'],ready=ready,heartbeats=beats,other_services_unchanged=True);d.save(folder/'report.json',d.wire(report)+b'\n')
 print(json.dumps({'state':report['state'],'new_cid':new['Id'],'report':str(folder/'report.json')}))
if __name__=='__main__':main()
