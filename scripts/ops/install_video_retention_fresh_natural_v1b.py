"""Install lookup-only source maintenance after genuine formal Natural cutover.

Default plan; only the video service gains five tested RO source mounts. No
API client, user authorization creation, job reset, remote delete or forced
rollback. Installation never enables the timer. --enable-timer requires a real
prior run that refreshed public playback and deleted exactly three bound files.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import argparse, hashlib, json, os, re, subprocess, types, uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub')
BASE=Path('/usr/local/lib/lighthouse-backup-ops/video-retention-fresh-natural-v1b')
STATE=Path('/var/lib/lighthouse-backup-ops/video-retention-fresh-natural-v1b')
RELEASE=ROOT/'config/video-source-retention-fresh-natural-v1b-release.json'
SERVICE=Path('/etc/systemd/system/lighthouse-video-retention-fresh-natural-v1b.service')
TIMER=Path('/etc/systemd/system/lighthouse-video-retention-fresh-natural-v1b.timer')
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
ARCHIVE='741272248ef37694d90d2b382b53609c4dd28c516867a2510d22f14bb2a703b5'
NATURAL_INSTALLER_SHA='6bf479fe25313ee2845702dafe59c51f2d7e6241fd0fb245d168f52a92f81e6f'
EXTRA=('services/hub/archive_source_retention_v2.py','services/hub/video_source_retention_v2.py',
 'services/hub/video_source_retention_natural_formats_v3.py','services/hub/video_retention_scheduler_v2.py',
 'services/hub/video_retention_refresh_v1.py')
SOURCE_PINS={'adapters/bilibili_video_guard.mjs': '1ecb678df412dd4aeb2680b4762e82b04bf7083484da81ee8df90815fc43069c', 'adapters/browser_replay_video.mjs': '61a249b918af9371858f677dc2629ab9d3898453b02c930b3aa9236004aa7f1a', 'adapters/media_tools.py': '434c5c098a6dd3520b88f57823408d50dc46108438c22e081fed9be90876d4e8', 'adapters/recovered_submit_gate.mjs': '6933998117e6b1c3e8c5ff455ca7a616233831fae62565ee9512a8f3d4cc277a', 'adapters/replay_highest_contract_v2.mjs': '3f2bd06a3dbb379ad4db675f61b37de2162f9e232805cc23950be504e936feff', 'adapters/replay_native_driver.mjs': '4c8dbdea7d04ab2192552f7b9cc1ad370d6cec2d6ba76121342d600af3eafddb', 'adapters/replay_natural_contract_v3.mjs': '0888e2e9b4a089c833237984e12179f06129dfaa3126e607f93db96f46bf2adc', 'adapters/safe_https.py': '8d393985e41f2409e9a1fa738e3ed2d2842d7dc4bd02ebe9c79a25bb9b01149f', 'adapters/short_video_metadata_chain.mjs': '0a096995b314afe88e07f9afd082d1072704cfb519a1d33283067417d4ebae89', 'adapters/short_video_saved_draft_navigation_control.mjs': '4a97c6c9833a9c58705492ccdbd12db2ac00cbbd564f100e8324085e8d09ef0e', 'adapters/youtube.py': '82a1d645a342665188e6cbe53cc2a821b3ff57492e3360f3427ba403dfae1fb0', 'adapters/youtube_feed.py': '718d398f92eb425c2ae4165a9756b05840820325f0f4d665b06398e5f26ed5aa', 'adapters/youtube_readiness_evidence_v3.py': '1899af9bac102152dda8a724f1da22ddaf715c82fff32726d82a1752b31fa9a9', 'adapters/ytdlp_highest_v2.py': '4bd41c848335e9a93bcccd985a159f33dfe6d2ef284e2dda43df2c46a4cadff8', 'adapters/ytdlp_natural_plan_v3.py': '7fa9553b2f4d24d9d6412937b49980c48443e60019757f20d2b4fbe4891f193d', 'adapters/ytdlp_natural_readiness_v3.py': 'ccb6a09122f87450704da5bf505f3b83525a8df9b7463c7352924e959f38e1fa', 'adapters/ytdlp_natural_v3.py': '7ce79506d7e2fa52d63935c80a7c4a0a87f4f92f4fd79908ac6bd8ad0cecf1a0', 'adapters/ytdlp_replay.py': '2d0e5867b870ba28d5595128488e0d895dcd042a38aa110266e42dfb302b8b8f', 'scheduler/service.py': '53412a81121d98790e4a7a1d425de7517135afea8cb4bf5d83144288a4e9cd97', 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs': 'f4e74eb6ca72012a44d72d8d1b29900662e001010e51feb0cbf5b461b491fcc6', 'services/hub/archive_source_retention_v2.py': 'cb81506a9fc0b65a85f25c3f4417496a50fb0bd22e216b4003826d11f2bb2a5e', 'services/hub/budget.py': '1de68d1a9937e229be76fc5fe2de21ae1814a9cfc0158096f86bc67bd87f827e', 'services/hub/config.py': 'f3d039c30fce7aa0051f597b1830fed5170bf715e0af6151eaef774b83ed8ec2', 'services/hub/cover_normalizer_v4.py': 'bbc133b7469578650069d6ecc32b593c6b141a55e56afcdb52938eb2558b00ae', 'services/hub/db.py': 'a5c8134ba961621793e7f4ad536d785d936e8f53ff439add448d7b0223c48e3d', 'services/hub/errors.py': 'e33f726574126cf7dfbbafbdaeb514aa8d92addc66801f6282b5c01a38c8039b', 'services/hub/evidence.py': '1ede4fb0b89951394f50f3e31e2e80e579196dac7b9fac9284cd0fafdce567a2', 'services/hub/locks.py': 'ca7433c198f96cfdfa0c5aaea99dc789354a19d60e0f5ea59b21a6b4063dace9', 'services/hub/maintenance.py': '7f78219d4a0e097b95791e655e31b766befb6d3ce393b8c90b10874784c03f03', 'services/hub/media_processing.py': 'cdd769af85dddfbdf7b736f38280e2aa90d5dcd4302d3cf09a16a9b46ca63869', 'services/hub/outcomes.py': '3a55475a66626538afa0e6215964bff7e6cafedf367a934173ced5a7893c16a1', 'services/hub/recovered_submit_ticket.py': 'cdb31fbd7542c6a61272751c446d5650406d75f329e9a48215f9a2259a096e12', 'services/hub/replay_highest_coverage_v2.py': 'dbba8457a11c01f46d64364993f3a5c19ddfe0a81149b8b1dfc980f96f31602e', 'services/hub/replay_highest_limits_v2.py': 'e6bc3b1ac860797b4f786a69ea39b7443302f2a5bcad0541f70e8faf95f688b5', 'services/hub/replay_highest_video_v2.py': '2481d6a2122b270f7bd92d4d9c639666d973ac62f1a49848ba4621398e28aac0', 'services/hub/replay_highest_worker_runtime_v2.py': 'e43b25f10145e755f62671c22f70b2d70c8e73f3060f66c60105c382b5980235', 'services/hub/replay_machine_checks.py': '1879757741d61173c9b5a27e6e3a210294b4c3efa9952e6b9483e97dd1fb6588', 'services/hub/replay_moderation_common.py': 'beb29443f24316c37859d4fdba94f4a0a9ad505a327af58323ac30f26728c9e1', 'services/hub/replay_natural_launch_v3.py': 'cfc3b5a730ff0a9a38684cbd0c3a5383d238273bf011ffd1caacb457245cdb48', 'services/hub/replay_natural_moderation_v3.py': 'bb9d5492ad912aa2b74e49a405476ce7c9040f276ba00ab2b635853c34197beb', 'services/hub/replay_natural_video_v3.py': '572f59f809ea37bbed9e3fb893f1252182e22ec98c645ecc67fd3d3765f19c7c', 'services/hub/replay_natural_worker_runtime_v3.py': '73685a6f0f5b9a3b44de79e5d8dfd89972c86c40a4cd9a053f8430fcd044b0d0', 'services/hub/replay_packet_cache_v1.py': '7727417e5064cd669d416ce9cee4209caaddc8b3e84a59c490031ad5c6b2a2f1', 'services/hub/replay_video_browser.py': 'a7cd7e743d50c7be226c5b087422b825707676494df974d4c7027d5bc9d52446', 'services/hub/replay_video_cover.py': '51f4367d9fc7a94cb88d95a169f9dce1e105f585f0d4c18a79964880494e3527', 'services/hub/replay_video_web.py': '3fa3c426bf59c7aca523298091bf0f2267b56e4cc3a8cf7b23654e273ce222f8', 'services/hub/replay_worker_runtime.py': 'e593e5d9024007afa36adb9577f9dfe5d31ac8a627f85046c9413d4e716e70c2', 'services/hub/resource_wait_v1.py': '0527e716782a5217436822d4157f282b2ac235c49d7a070a9fe088b2cde2fb38', 'services/hub/short_video_web.py': '49e212ed3a3a6dab8daec3256aa649a1b714a8e7e92a382b9244e17148a5db71', 'services/hub/video_retention_refresh_v1.py': 'd86c3b34e71e9d4779abfc9ea158e6e755ae3906ce1f3a349f60e433d8c06b1e', 'services/hub/video_retention_scheduler_v2.py': 'e4b5f755a6bcf83c549744d121adbd30e14ecc44fa1dc7245162290e9fc7aec5', 'services/hub/video_source_retention_natural_formats_v3.py': '8ac970acbedff547d39ef55724303f9dc366171394ce3b853d85ac52123cb897', 'services/hub/video_source_retention_v2.py': '91c2031401d69c072f66d4bb1c7e88d2f85aac8e5396688df1678230d8229e4c', 'services/hub/worker.py': '7775aef7a559995724c9a6dfd1d5885d32b50499343db11ae34afe38bcfba310', 'services/hub/youtube_download.py': 'd14f7d8c522481a6070f86afd46f402b09b6277212821986e89eba3bf0f0337a', 'services/hub/youtube_download_v2.py': '76f14796fb3d9b9641099ef2cc59c38f207989ede8acd7e88d678e43315b52d6', 'services/hub/youtube_download_v3.py': 'fbabef820c284ed832671fc7b0bcab874b033c7bbae97ff7b62f8362fad87132', 'services/hub/youtube_highest_plan_v2.py': 'cb4cbeda699dd282cd196291c8cf2a16799ab9b2b00c4c268f7745a35f2e6091', 'services/hub/youtube_monitor.py': '5466acf7505c2808ea00e478836b76ad79ebf5d0693350f2da5be5b0739dc75b', 'services/hub/youtube_natural_plan_v3.py': 'c3bb5877d24b5788ede9a4ce95ceb88dc4de4a0b6b43396208bac2ab29074a66', 'services/hub/youtube_readiness_v3.py': '1e2173ee546a9f59a664148de1dbdab79f8f19e3dcf354bd315f420a09a31c93', 'vendor/subtitle-cloud-worker/observer/store.py': '5f89202fa37c09200236a940ea6f8ad396d389a19364ab8034a736bd53430673'}
GROUPS={'tests.test_video_source_retention_v2':19,'tests.test_video_source_retention_tracks_v2':13,
 'tests.test_video_retention_scheduler_v2':6,'tests.test_video_source_retention_natural_formats_v3':8,
 'tests.test_video_retention_refresh_v1':26}

def digest(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
def sha_argument(value):
 if not isinstance(value,str) or re.fullmatch('[a-f0-9]{64}',value) is None:raise argparse.ArgumentTypeError('Expected exact 64 lowercase hexadecimal SHA256')
 return value

def load_natural(file):
 file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
 raw=file.read_bytes();assert digest(raw)==NATURAL_INSTALLER_SHA
 module=types.ModuleType('frozen_natural_operator');module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module

def gate(directory,expected_sha,m):
 m.protected(directory)
 for name in ('test-attestation.json','container.json','overlay/manifest.json'):m.protected(directory/name)
 raw=(directory/'test-attestation.json').read_bytes();assert digest(raw)==sha_argument(expected_sha)
 value=json.loads(raw);manifest=value['manifest']
 assert value['version']=='video-source-retention-fresh-natural-v1b-ubuntu-tests' and value['image']==IMAGE and value['archive_sha256']==ARCHIVE
 assert value['tests']==72 and value['exit_code']==value['failures']==value['errors']==value['skipped']==0 and value['oom_killed'] is False
 for key in ('full_suite_passed','runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'):assert value[key] is True
 for key in ('whole_tree_deployed','application_data_mounted','production_profile_mounted','secrets_environment_injected'):assert value[key] is False
 assert value['network']=='none' and manifest['version']=='video-retention-fresh-natural-ro-overlay-v1b' and len(manifest['files'])==130
 assert manifest['parent_natural_295_archive_sha256']=='8fc11a3e27dca0dd2c08c12a34da111e70f37bc38ce74d6cdebb122840578e2a'
 assert manifest['retention_refresh_commit']=='45995b47ed09a69bf8a7d73847f4851de84ef28b'
 assert manifest['native_freshness_seconds_unchanged']==300 and manifest['busy_locks_consume_platform_attempts'] is False
 assert len(manifest['parent_natural_runtime_files'])==57 and len(manifest['release_source_files'])==62
 assert set(manifest['release_source_files'])==set(manifest['parent_natural_runtime_files'])|set(EXTRA)
 assert value['source_files']==SOURCE_PINS=={name:manifest['files'][name]['sha256'] for name in manifest['release_source_files']}
 junit=directory/'accepted-results.xml';m.protected(junit);jraw=junit.read_bytes()
 assert value['junit_file']==str(junit) and digest(jraw)==value['junit_sha256']
 cases=list(ET.fromstring(jraw).iter('testcase'));counts={}
 for case in cases:
  counts[case.attrib['classname']]=counts.get(case.attrib['classname'],0)+1
  assert all(case.find(tag) is None for tag in ('failure','error','skipped'))
 assert len(cases)==72 and counts==GROUPS
 assert json.loads((directory/'overlay/manifest.json').read_bytes())==manifest
 bodies={}
 for name,entry in manifest['files'].items():
  assert not Path(name).is_absolute() and '..' not in Path(name).parts
  file=directory/'overlay'/name;m.protected(file);body=file.read_bytes()
  assert 0<len(body)<500000 and len(body)==entry['bytes'] and digest(body)==entry['sha256'];bodies[name]=body
 container=json.loads((directory/'container.json').read_bytes());host=container['HostConfig']
 assert container['Id']==value['container_id'] and container['Image']==IMAGE and container['Config']['User']=='10001:10001'
 assert container['State']['ExitCode']==0 and container['State']['OOMKilled'] is False and host.get('Init') is True
 assert host['ReadonlyRootfs'] is True and host['NetworkMode']=='none' and host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
 ident=directory.name;assert str(uuid.UUID(ident))==ident
 owned=Path('/home/lighthouse-ops')/('lighthouse-video-retention-fresh-natural-v1b-owned-'+ident)
 binds=[row for row in container['Mounts'] if row['Type']=='bind']
 expected={str(owned),str(directory/'check-and-test.py'),str(directory/'overlay/manifest.json')}|{str(directory/'overlay'/name) for name in manifest['files']}
 assert len(binds)==len(expected) and {row['Source'] for row in binds}==expected
 assert all(row['RW'] is False or row['Source']==str(owned) for row in binds)
 assert all(row['Type']=='bind' or row['Type']=='tmpfs' and row['Destination']=='/tmp' for row in container['Mounts'])
 for label in ('before','after'):
  item=value['resource_snapshot_'+label]
  assert int(item['pids.events'].get('max',0))==int(item['memory.events'].get('oom',0))==int(item['memory.events'].get('oom_kill',0))==0
 return value,bodies


def proof_mounts(d,proof_directory):
 return [d.bind(BASE/'proof'/name,proof_directory/name,True) for name in ('test-attestation.json','accepted-results.xml')]


def candidate(before,d,extra_root,proof_directory):
 value=deepcopy(before);service=value['services']['video-worker'];existing={v['target'] for v in service['volumes']}
 assert service['command']==['python','-m','services.hub.replay_natural_worker_runtime_v3']
 for name in EXTRA:
  assert str(ROOT/name) not in existing
  service['volumes'].append(d.bind(extra_root/name,ROOT/name,True))
 for item in proof_mounts(d,proof_directory):
  assert item['target'] not in {v['target'] for v in service['volumes']}
  service['volumes'].append(item)
 assert all(value['services'][name]==before['services'][name] for name in before['services'] if name!='video-worker')
 return value


PRECHECK=r"""
import hashlib,json,os,socket,threading,sys
assert os.getuid()==os.getgid()==10001
def deny(*a,**k):raise RuntimeError('FRESH_RETENTION_PREFLIGHT_NETWORK_OR_THREAD_DENIED')
socket.socket.connect=deny;threading.Thread.start=deny
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.replay_natural_video_v3 import NaturalReplayVideoWeb
s=Settings.load();db=Store(s.root/'data/hub.sqlite3');service=NaturalReplayVideoWeb(s,db)
policy=service.policy();grant=service._require_workflow_launch(policy)
rows=db.query('SELECT record FROM video_retention_authorizations_v2 WHERE id=?',(sys.argv[1],));assert len(rows)==1
record=json.loads(rows[0]['record']);assert record['authorization_id']==sys.argv[1] and record['scope']=='confirmed_full_duration_public_video'
assert record['target_site_uid']==s.config['target_bilibili_site_uid'] and record['publication_authorized'] is False
print(json.dumps({'formal_natural_workflow_verified':True,'authorization_id':sys.argv[1],
 'authorization_sha256':hashlib.sha256(encode(record).encode()).hexdigest(),'policy_sha256':policy['_sha256'],
 'jobs_claimed':0,'native_requests':0,'human_review':False}))
"""


def validate_current(d,m,operator,before,current,proof,bodies,proof_directory,*,installed=False):
 names=proof['manifest']['parent_natural_runtime_files'];code={name:bodies[name] for name in names}
 key=[v['source'] for v in before['services']['video-worker']['volumes'] if v['target']=='/run/secrets/youtube-api-key'];assert len(key)==1
 key=d.key_file(key[0]);code_root=d.INSTALL/'natural-v3b-cache'/m.CACHE_RELEASE/'code'
 expected=m.proposed_service(d,key,code,code_root,operator)
 if installed:expected['volumes'] += [d.bind(BASE/'code'/name,ROOT/name,True) for name in EXTRA]+proof_mounts(d,proof_directory)
 for v in expected['volumes']:v.setdefault('read_only',False)
 operator.validate_running(d,current,expected);m.verify_runtime_environment(d,current['Id'],operator)
 for name in names:m.protected(code_root/name);assert d.read(code_root/name,500000)==bodies[name]
 if installed:
  for name in EXTRA:m.protected(BASE/'code'/name);assert d.read(BASE/'code'/name,500000)==bodies[name]
  for name in ('test-attestation.json','accepted-results.xml'):
   m.protected(BASE/'proof'/name);assert d.read(BASE/'proof'/name,1000000)==(proof_directory/name).read_bytes()
 return expected


RELEASE_CHECK=r"""
import json,os,sys
assert os.getuid()==os.getgid()==10001
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.video_retention_refresh_v1 import FreshNaturalVideoSourceRetentionV1
s=Settings.load();store=Store(s.root/'data/hub.sqlite3');service=FreshNaturalVideoSourceRetentionV1(s,store)
authorization=service._authorization(sys.argv[1]);assert authorization['scope']=='confirmed_full_duration_public_video'
checksum=service._release({'authorization_id':sys.argv[1],'scope':authorization['scope'],
 'proof':{'pipeline_state':'replay_natural_video_web_v3'}})
print(json.dumps({'app_release_verified':True,'release_sha256':checksum,'files_deleted':0,'native_requests':0}))
"""


def verify_app_release(d,container_id,authorization_id):
 value=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',container_id,'python','-c',RELEASE_CHECK,authorization_id],timeout=90))
 assert value['app_release_verified'] is True and value['files_deleted']==value['native_requests']==0
 sha_argument(value['release_sha256']);return value


def maintenance_once(d,m,container_id,authorization_id):
 ident=str(uuid.uuid4());receipt={'state':'unknown','at':datetime.now(timezone.utc).isoformat(),
  'authorization_id':authorization_id,'local_files_deleted':0,'operation_id':ident}
 try:
  value=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',container_id,'python','-m','services.hub.video_retention_refresh_v1',
   '--authorization-id',authorization_id,'--max-jobs','1','--apply'],timeout=7200))
  assert value['version']=='completed-video-retention-refresh-v1' and value['authorization_id']==authorization_id
  assert value['apply'] is True and value['max_jobs']==1 and len(value['processed'])<=1
  deleted=sum(row['result'].get('local_files_deleted',0) for row in value['processed'])
  assert type(deleted) is int and 0<=deleted<=3
  receipt.update(state='completed',local_files_deleted=deleted,maintenance=value)
 except BaseException as exc:
  receipt['error_class']=type(exc).__name__
  m.install_file(d,STATE/'run-history'/(ident+'.json'),wire(receipt),mode=0o600,group=0)
  m.install_file(d,STATE/'last-run.json',wire(receipt),mode=0o600,group=0)
  raise RuntimeError('FRESH_RETENTION_MAINTENANCE_RESULT_UNKNOWN') from None
 m.install_file(d,STATE/'run-history'/(ident+'.json'),wire(receipt),mode=0o600,group=0)
 m.install_file(d,STATE/'last-run.json',wire(receipt),mode=0o600,group=0)
 return receipt


def post_start_acceptance(d,m,container_id,processes,started,report,evidence,*,release_check=lambda:None):
 try:
  if started is not None:
   import time
   deadline=time.monotonic()+60
   while True:
    beats=m.database_status(d,'594aed06-3aa1-45f4-90f0-1db49a602395')['heartbeats']
    if all(beats.get(name,0)>=started for name in ('worker:video','worker:video-main-v3','worker:video-moderation-v3')):break
    assert time.monotonic()<deadline and d.inspect(container_id)['State']['Running'];time.sleep(1)
  assert m.core_processes(d)==processes
  report['app_release']=release_check()
 except BaseException as exc:
  report.update(state='requires_reconciliation',error_class=type(exc).__name__,automatic_rollback_performed=False)
  m.install_file(d,evidence/'result.json',wire(report),mode=0o600,group=0)
  raise RuntimeError('FRESH_RETENTION_POST_START_REQUIRES_RECONCILIATION') from None
 m.install_file(d,evidence/'result.json',wire(report),mode=0o600,group=0)


def units():
 service='[Unit]\nDescription=Lighthouse completed-video fresh lookup and exact source retention\nAfter=docker.service\nRequires=docker.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 '+str(BASE/'operator.py')+' --run-once\nTimeoutStartSec=7300\nUMask=0077\n'
 timer='[Unit]\nDescription=Bounded completed-video source maintenance\n[Timer]\nOnBootSec=15min\nOnUnitInactiveSec=30min\nPersistent=true\nUnit='+SERVICE.name+'\n[Install]\nWantedBy=timers.target\n'
 return service.encode(),timer.encode()


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--proof-directory');parser.add_argument('--proof-sha256',type=sha_argument)
 parser.add_argument('--natural-installer-file');parser.add_argument('--deployer-file');parser.add_argument('--cache-operator-file')
 parser.add_argument('--authorization-id');parser.add_argument('--install',action='store_true');parser.add_argument('--run-once',action='store_true');parser.add_argument('--enable-timer',action='store_true')
 a=parser.parse_args();assert os.name=='posix' and os.geteuid()==0 and sum((a.install,a.run_once,a.enable_timer))<=1
 if a.run_once or a.enable_timer:
  config_file=BASE/'runtime.json';config=json.loads(config_file.read_bytes());m=load_natural(config['natural_installer_file']);m.protected(config_file)
  assert digest(Path(__file__).read_bytes())==config['operator_sha256']
  for key in ('proof_directory','proof_sha256','natural_installer_file','deployer_file','cache_operator_file','authorization_id'):setattr(a,key,config[key])
 else:assert all(getattr(a,key) for key in ('proof_directory','proof_sha256','natural_installer_file','deployer_file','cache_operator_file','authorization_id'))
 assert str(uuid.UUID(a.authorization_id))==a.authorization_id
 m=load_natural(a.natural_installer_file);d=m.helper(a.deployer_file);operator=m.cache_operator(a.cache_operator_file)
 proof_dir=Path(a.proof_directory);proof,bodies=gate(proof_dir,a.proof_sha256,m)
 original=d.read(ROOT/'compose.yaml');original_stat=(ROOT/'compose.yaml').stat();m.protected(ROOT/'compose.yaml')
 before=d.compose_config();current=d.inspect(d.container_id('video-worker'));processes=m.core_processes(d)
 installed=any(v['target']==str(ROOT/'services/hub/video_retention_refresh_v1.py') for v in before['services']['video-worker']['volumes'])
 expected=validate_current(d,m,operator,before,current,proof,bodies,proof_dir,installed=installed)
 accepted=json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',PRECHECK,a.authorization_id],timeout=90))
 assert accepted['formal_natural_workflow_verified'] is True
 release={'version':'video-source-retention-release-v2','scope':'confirmed_full_duration_public_video','authorization_id':a.authorization_id,
  'source_files':proof['source_files'],'test_attestation':{'file':str(proof_dir/'test-attestation.json'),'sha256':a.proof_sha256}}
 report={'version':'fresh-natural-retention-install-v1','state':'verified_plan_only','installed':False,'timer_enabled':False,
  'native_requests':0,'local_files_deleted':0,'publication_authorization_created':False,'other_core_processes':processes,
  'source_files':proof['source_files'],'authorization':accepted,'test_attestation_sha256':a.proof_sha256}
 if installed:verify_app_release(d,current['Id'],a.authorization_id)
 if a.enable_timer:
  assert installed;file=STATE/'last-run.json';m.protected(file);last=json.loads(file.read_bytes())
  assert last['state']=='completed' and last['authorization_id']==a.authorization_id and last['local_files_deleted']>=3
  assert digest(RELEASE.read_bytes())==config['release_sha256']
  d.run(['/usr/bin/systemctl','enable','--now',TIMER.name]);report.update(state='timer_enabled_after_actual_retention_acceptance',installed=True,timer_enabled=True)
 elif a.run_once:
  assert installed and digest(RELEASE.read_bytes())==config['release_sha256']
  receipt=maintenance_once(d,m,current['Id'],a.authorization_id)
  report.update(state='maintenance_completed',installed=True,maintenance=receipt['maintenance'],local_files_deleted=receipt['local_files_deleted'])
 elif a.install:
  import fcntl
  fd=os.open('/run/lighthouse-natural-replay-v3-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);locks=[]
  evidence=STATE/'install-history'/str(uuid.uuid4())
  m.install_file(d,evidence/'intent.json',wire({**report,'state':'install_intent','compose_before_sha256':digest(original)}),mode=0o600,group=0)
  m.install_file(d,evidence/'compose-before.yaml',original,mode=0o600,group=0)
  started=None
  try:
   for role in ('video','moderation'):
    lock=m.role_lock(d,role)
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:os.close(lock);raise
    locks.append(lock)
   assert m.database_status(d,'594aed06-3aa1-45f4-90f0-1db49a602395')['video_processing']==0
   assert d.read(ROOT/'compose.yaml')==original and m.core_processes(d)==processes
   for name in EXTRA:m.install_file(d,BASE/'code'/name,bodies[name],mode=0o644,group=0)
   for name in ('test-attestation.json','accepted-results.xml'):
    m.install_file(d,BASE/'proof'/name,(proof_dir/name).read_bytes(),mode=0o644,group=0)
   m.install_file(d,RELEASE,wire(release))
   payload={key:getattr(a,key) for key in ('proof_directory','proof_sha256','natural_installer_file','deployer_file','cache_operator_file','authorization_id')}
   payload.update(operator_sha256=digest(Path(__file__).read_bytes()),release_sha256=digest(wire(release)))
   m.install_file(d,BASE/'operator.py',Path(__file__).read_bytes(),mode=0o644,group=0);m.install_file(d,BASE/'runtime.json',wire(payload),mode=0o600,group=0)
   if not installed:
    proposed=candidate(before,d,BASE/'code',proof_dir);d.compose('stop','--timeout','45','video-worker')
    assert not d.inspect(current['Id'])['State']['Running']
    d.atomic(ROOT/'compose.yaml',d.wire(proposed),original=original_stat);d.assert_other_services(before,d.compose_config())
    import time
    started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker')
    current=d.inspect(d.container_id('video-worker'));validate_current(d,m,operator,d.compose_config(),current,proof,bodies,proof_dir,installed=True)
   service,timer=units();m.install_file(d,SERVICE,service,mode=0o644,group=0);m.install_file(d,TIMER,timer,mode=0o644,group=0)
   d.run(['/usr/bin/systemctl','daemon-reload']);assert m.core_processes(d)==processes
   report.update(state='installed_timer_not_enabled',installed=True,video_container_id=current['Id'])
  except BaseException as exc:
   report.update(state='requires_reconciliation',error_class=type(exc).__name__,automatic_rollback_performed=False)
   m.install_file(d,evidence/'result.json',wire(report),mode=0o600,group=0)
   raise RuntimeError('FRESH_RETENTION_INSTALL_REQUIRES_RECONCILIATION') from None
  finally:
   for lock in reversed(locks):os.close(lock)
   os.close(fd)
  post_start_acceptance(d,m,current['Id'],processes,started,report,evidence,
   release_check=lambda:verify_app_release(d,current['Id'],a.authorization_id))
 print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
