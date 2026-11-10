"""Owned source composition and command guards; no Docker, account or writes."""
from copy import deepcopy
from pathlib import Path,PurePosixPath
from types import SimpleNamespace
import ast,contextlib,importlib.util,json
import pytest
from ops.historical_artifacts import historical_member

REPO=Path(__file__).resolve().parents[1]
def load(name):
 spec=importlib.util.spec_from_file_location(name,REPO/'scripts/ops'/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
d=load('deploy_natural_replay_v3b_cache');c=load('issue_natural_candidate_v3b_cache')

def composition():
 machine,natural='services/hub/replay_machine_checks.py','services/hub/replay_natural_video_v3.py'
 baseline={machine:{'sha256':'old-machine'},natural:{'sha256':'unchanged'},'tests/test_replay_machine_checks.py':{'sha256':'old-test'}}
 base={'archive_sha256':'owned85','manifest':{'files':deepcopy(baseline)}}
 download={'manifest':{'source_commit':d.RELEASE,'files':{natural:deepcopy(baseline[natural])}}}
 integration={'archive_sha256':'owned21','manifest':{'source_freeze_commit':d.RELEASE,'previous_85_archive_sha256':'owned85',
  'files':deepcopy(baseline),'changed_from_85':[]}}
 new=deepcopy(baseline)
 for name,sha in d.CACHE_PINS.items():new[name]={'sha256':sha}
 new['tests/test_replay_machine_checks.py']={'sha256':'owned-new-test'}
 for name in ('tests/test_replay_packet_cache_v1.py','docs/REPLAY_PACKET_CACHE_V1.md'):new[name]={'sha256':'owned-new'}
 changes=list(d.CACHE_PINS)+['tests/test_replay_machine_checks.py','tests/test_replay_packet_cache_v1.py','docs/REPLAY_PACKET_CACHE_V1.md']
 cache={'manifest':{'files':new,'source_freeze_commit':d.CACHE_RELEASE,'parent_21_archive_sha256':'owned21',
  'previous_85_archive_sha256':'owned85','production_runtime_changed_files':list(d.CACHE_PINS),'changed_files':changes,
  'native_freshness_seconds_unchanged':300,'native_observed_at_changed':False,'source_current_full_sha_required_each_call':True}}
 return base,download,integration,cache


def test_exact_two_runtime_files_compose_over_original_21_without_changing_other_sources():
 values=composition();new=d.composed_sources(*values)
 assert new['services/hub/replay_natural_video_v3.py']['sha256']=='unchanged'
 assert all(new[name]['sha256']==sha for name,sha in d.CACHE_PINS.items())
 assert len(d.CACHE_PINS)==2 and d.ENTRY=='services.hub.replay_natural_worker_runtime_v3'


@pytest.mark.parametrize('change',['hidden_runtime','wrong_machine','freshness','observed_at','skip_current_hash','wrong_parent'])
def test_cache_source_binding_cannot_hide_another_change_or_relax_native_freshness(change):
 values=composition();cache=values[-1]['manifest']
 if change=='hidden_runtime':cache['files']['services/hub/replay_natural_video_v3.py']['sha256']='unapproved-change'
 elif change=='wrong_machine':cache['files']['services/hub/replay_machine_checks.py']['sha256']='wrong-hash'
 elif change=='freshness':cache['native_freshness_seconds_unchanged']=900
 elif change=='observed_at':cache['native_observed_at_changed']=True
 elif change=='skip_current_hash':cache['source_current_full_sha_required_each_call']=False
 else:cache['parent_21_archive_sha256']='other21'
 with pytest.raises(AssertionError):d.composed_sources(*values)


def pinned_operator():return d.cache_operator(REPO/'scripts/ops/apply_replay_packet_cache_v1.py')

def fake_deployer(tmp_path,base_service):
 return SimpleNamespace(ROOT=tmp_path/'root',new_video_service=lambda key,code,code_root:deepcopy(base_service),
  bind=lambda source,target,readonly:{'source':str(source),'target':str(target),'read_only':readonly})


def test_three_fixed_private_runtime_directories_are_declared_without_changing_entry_environment_or_key(tmp_path):
 operator=pinned_operator();base={'image':'owned-image','environment':{'HOME':'/tmp/highest/home','XDG_CONFIG_HOME':'/tmp/highest/config','XDG_CACHE_HOME':'/tmp/highest/cache'},
  'volumes':[{'source':'owned-key','target':'/run/secrets/youtube-api-key','read_only':True}],
  'tmpfs':['/tmp:rw,size=192m,mode=1777','/owned/profiles:rw,size=1m,mode=0700,uid=10001,gid=10001']}
 value=d.proposed_service(fake_deployer(tmp_path,base),'owned-key',{},tmp_path/'code',operator)
 assert value['image']==base['image'] and value['environment']==base['environment'] and value['volumes'][0]==base['volumes'][0]
 assert value['command']==['python','-m',d.ENTRY]
 assert value['tmpfs']==base['tmpfs']+[path+':'+options for path,options in operator.DIRECTORIES.items()]
 assert all('uid=10001,gid=10001' in opts and 'mode=0700' in opts for opts in operator.DIRECTORIES.values())
 assert 'mkdir' not in operator.VERIFY_ENV


def test_duplicate_environment_tmpfs_is_rejected(tmp_path):
 operator=pinned_operator();base={'tmpfs':['/tmp/highest/home:rw,size=1m'],'volumes':[]}
 with pytest.raises(AssertionError):d.proposed_service(fake_deployer(tmp_path,base),'key',{},tmp_path/'code',operator)


def test_real_app_environment_observation_must_succeed_instead_of_mkdir(tmp_path):
 operator=pinned_operator();calls=[]
 fake=SimpleNamespace(run=lambda command:(calls.append(command) or json.dumps({'persistent_runtime_environment_directories_verified':True})))
 assert d.verify_runtime_environment(fake,'owned-container',operator)['persistent_runtime_environment_directories_verified']
 assert calls[0]==['/usr/bin/docker','exec','--user','10001:10001','owned-container','python','-c',operator.VERIFY_ENV]
 fake.run=lambda command:json.dumps({'persistent_runtime_environment_directories_verified':False})
 with pytest.raises(AssertionError):d.verify_runtime_environment(fake,'owned-container',operator)


def test_cache_operator_uses_exact_hashed_bytes_without_second_read(tmp_path,monkeypatch):
 file=tmp_path/'owned-operator.py';raw=b'VALUE=7\n';file.write_bytes(raw)
 monkeypatch.setattr(d,'CACHE_OPERATOR_SHA',d.digest(raw));original=Path.read_bytes
 def swap(path):
  body=original(path)
  if path==file:file.write_bytes(b'VALUE=999\n')
  return body
 monkeypatch.setattr(Path,'read_bytes',swap)
 assert d.cache_operator(file).VALUE==7


def test_real_pilot_issuer_schema_network_and_queue_fences_are_identical_to_original():
 oldspec=importlib.util.spec_from_file_location('owned_original_issuer',REPO/'scripts/ops/issue_natural_candidate_v3.py')
 old=importlib.util.module_from_spec(oldspec);oldspec.loader.exec_module(old)
 assert c.ISSUE==old.ISSUE and c.PILOT==old.PILOT
 original=historical_member('historical-deployment-source-inputs.tar',
     'scripts/ops/'+Path(d.__file__).name,c.INSTALLER_SHA)
 assert c.INSTALLER_SHA==d.digest(original)
 # Today's changed installer must not inherit a historical release's approval.
 with pytest.raises(AssertionError):c.load(Path(d.__file__).resolve())
 assert c.DESTINATION!=old.DESTINATION
 assert 'actual_owner_launch_response=auth' in c.ISSUE and 'db.get(pilot)==prior' in c.ISSUE


def test_actual_295_scope_is_independent_and_not_claimed_by_old_21():
 spec=d.SPECS['cache'];assert spec[4:6]==(295,119)
 assert spec[0]=='replay-packet-cache-v1-ubuntu-tests' and d.SPECS['integration'][4]==21
 assert d.RELEASE!=d.CACHE_RELEASE
 assert d.CACHE_OPERATOR_SHA==d.digest((REPO/'scripts/ops/apply_replay_packet_cache_v1.py').read_bytes())
 assert d.CURRENT_OPERATOR_COMMIT=='6a1513c'


def test_current_operator_is_exact_deployed_version_not_obsolete_guard():
 operator=pinned_operator()
 assert operator.__name__=='pinned_packet_cache_operator'
 assert operator.COMMIT==d.CACHE_RELEASE
 assert callable(operator.mixed_gate) and callable(operator.validate_mixed_sources)
 assert d.CACHE_OPERATOR_SHA=='3b6093639434dae80cadbe65392f444f7f45b3b4455d1ea53fd2ab4688e991b8'


def test_current_45_sources_must_match_separately_tested_v2_mix(tmp_path):
 operator=pinned_operator();names=['owned-'+str(i) for i in range(45)]
 files={name:tmp_path/name for name in names}
 for name,file in files.items():file.write_bytes(name.encode())
 mixed={'manifest':{'candidate_runtime_source_sha256':{name:d.digest(file.read_bytes()) for name,file in files.items()}}}
 host=SimpleNamespace(read=lambda path,limit:path.read_bytes(),sha=d.digest)
 operator.validate_mixed_sources(host,files,mixed)
 files[names[0]].write_bytes(b'unreviewed')
 with pytest.raises(AssertionError):operator.validate_mixed_sources(host,files,mixed)


def test_current_cache_branch_validates_exact_mix_before_cutover(tmp_path):
 root=tmp_path/'root';seen=[];sources={'owned-current-source':tmp_path/'source'};mixed={'owned':'actual21'}
 operator=SimpleNamespace(current_sources=lambda *args:(sources,True),validate_mixed_sources=lambda host,actual,proof:seen.append((actual,proof)))
 host=SimpleNamespace(ROOT=root,INSTALL=tmp_path/'install')
 before={'services':{'video-worker':{'volumes':[{'target':str(root/'services/hub/replay_packet_cache_v1.py')}]}}}
 d.current_gate(host,before,{}, {},operator,mixed)
 assert seen==[(sources,mixed)]


def test_pre_cache_runtime_cannot_be_used_as_current_45_baseline(tmp_path):
 host=SimpleNamespace(ROOT=tmp_path/'root',INSTALL=tmp_path/'install')
 with pytest.raises(AssertionError):d.current_gate(host,{'services':{'video-worker':{'volumes':[]}}},{},{},SimpleNamespace(),{})


@pytest.mark.parametrize('change',['none','wrong_uid','extra_tmpfs'])
def test_strict_tmpfs_validation_removes_only_three_verified_additions(tmp_path,change):
 operator=pinned_operator();root=PurePosixPath('/owned/root');base={'/tmp':'rw,size=192m,mode=1777',str(root/'data/browser-profiles'):'rw,size=1m,mode=0700,uid=10001,gid=10001'}
 actual={**base,**operator.DIRECTORIES}
 if change=='wrong_uid':actual['/tmp/highest/home']=actual['/tmp/highest/home'].replace('uid=10001','uid=0')
 if change=='extra_tmpfs':actual['/tmp/not-approved']='rw,size=1m'
 service={'tmpfs':[name+':'+value for name,value in actual.items()]};inspected={'HostConfig':{'Tmpfs':actual}};saved=deepcopy(inspected);seen=[]
 host=SimpleNamespace(ROOT=root,validate_running=lambda normalized,service,code:seen.append(normalized))
 if change=='none':
  operator.validate_running(host,inspected,service);assert set(seen[0]['HostConfig']['Tmpfs'])==set(base)
 else:
  with pytest.raises(AssertionError):operator.validate_running(host,inspected,service)
  assert seen==[]
 assert inspected==saved


def test_offline_preflight_declares_private_leaves_and_does_not_start_worker_or_modify_live_db(tmp_path,monkeypatch):
 operator=pinned_operator();seen=[]
 @contextlib.contextmanager
 def owned_temp(**kwargs):
  folder=tmp_path/'owned-preflight';folder.mkdir();yield str(folder)
 monkeypatch.setattr(d.tempfile,'TemporaryDirectory',owned_temp)
 monkeypatch.setattr(d.os,'chown',lambda *a:None,raising=False)
 def snapshot(folder):
  file=folder/'hub.sqlite3';file.write_bytes(b'Owned consistent-copy boundary');return file,{'online_sqlite_backup':True}
 host=SimpleNamespace(ROOT=tmp_path/'root',online_snapshot=snapshot,run=lambda command,**kwargs:(seen.append(command) or json.dumps({'ready':True})))
 sources={'grant':tmp_path/'grant','snapshot':tmp_path/'snapshot',**{name:tmp_path/name for name in d.POLICIES}}
 d.preflight(host,tmp_path/'overlay',{},tmp_path/'owned-key',sources,b'owned=true',operator)
 command=seen[0];joined=' '.join(command)
 for path,value in operator.DIRECTORIES.items():assert path+':'+value in command
 assert '--network=none' in command and '--user' in command and '10001:10001' in command
 assert '-e' in command and 'HOME=/tmp/highest/home' in command and 'XDG_CONFIG_HOME=/tmp/highest/config' in command
 assert 'mkdir' not in operator.VERIFY_ENV
 check=command[-1];assert 'Store.claim=Store.recover=Store.enqueue=Store.finish=refuse' in check
 assert "Store('/run/preflight-db/hub.sqlite3')" in check and '/run/preflight-db' in joined
 assert '--env-file' not in command
