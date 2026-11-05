"""Pure root-operator planning/result boundaries; no Docker or systemd calls."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import argparse,json,uuid
import pytest
from scripts.ops import install_video_retention_fresh_natural_v1c as m


def base():
 return {'services':{'video-worker':{'command':['python','-m','services.hub.replay_natural_worker_runtime_v3'],
  'volumes':[{'source':'owned-profile','target':'owned-existing-profile','read_only':False}],
  'environment':{'HOME':'/tmp/highest/home'}},**{name:{'owned':name} for name in ('api','scheduler','tweet-worker','events')}}}


def helper():
 return SimpleNamespace(bind=lambda source,target,readonly:{'source':str(source),'target':str(target),'read_only':readonly})


def test_candidate_adds_exact_five_readonly_sources_and_preserves_other_services_and_environment(tmp_path):
 old=base();saved=deepcopy(old);new=m.candidate(old,helper(),tmp_path/'code',tmp_path/'original-proof')
 assert old==saved and new['services']['video-worker']['environment']==old['services']['video-worker']['environment']
 for name in ('api','scheduler','tweet-worker','events'):assert new['services'][name]==old['services'][name]
 extra=new['services']['video-worker']['volumes'][1:]
 assert len(extra)==7 and all(v['read_only'] is True for v in extra)
 assert {v['target'] for v in extra}=={str(m.ROOT/name) for name in m.EXTRA}|{str(tmp_path/'original-proof'/name) for name in ('test-attestation.json','accepted-results.xml')}


def test_duplicate_source_target_and_old_v2_entry_are_rejected(tmp_path):
 old=base();old['services']['video-worker']['volumes'].append({'target':str(m.ROOT/m.EXTRA[0])})
 with pytest.raises(AssertionError):m.candidate(old,helper(),tmp_path,tmp_path/'original-proof')
 old=base();old['services']['video-worker']['command'][-1]='services.hub.replay_highest_moderation_v2b'
 with pytest.raises(AssertionError):m.candidate(old,helper(),tmp_path,tmp_path/'original-proof')


@pytest.mark.parametrize('value',['0'*65,'A'*64,'a'*63,'not-a-hash'])
def test_exact_raw_sha_argument_cannot_accept_terminal_wrap_corruption(value):
 with pytest.raises(argparse.ArgumentTypeError):m.sha_argument(value)


def test_independent_source_gate_pins_actual_natural_and_retention_not_only_pass_boolean():
 assert len(m.SOURCE_PINS)==62 and sum(m.GROUPS.values())==74
 assert set(m.EXTRA)<=set(m.SOURCE_PINS)
 assert m.SOURCE_PINS['services/hub/video_retention_refresh_v1.py']=='f43e0d19d6bc494621f5eeff1687c6f7b5785810b5bc90185879c8b7071c17e7'
 assert m.SOURCE_PINS['services/hub/replay_machine_checks.py']=='1879757741d61173c9b5a27e6e3a210294b4c3efa9952e6b9483e97dd1fb6588'
 assert m.SOURCE_PINS['services/hub/replay_packet_cache_v1.py']=='7727417e5064cd669d416ce9cee4209caaddc8b3e84a59c490031ad5c6b2a2f1'
 assert m.SOURCE_PINS['services/hub/video_retention_scheduler_v2.py']=='194513fdc3fe96608c10ead71dc5bac0d4e44c1e651f9557bb552d0b7751aef1'
 assert 'vendor/subtitle-cloud-worker/observer/store.py' in m.SOURCE_PINS and 'scheduler/service.py' in m.SOURCE_PINS


def test_timer_uses_existing_app_runtime_without_key_or_parallel_worker():
 service,timer=m.units();text=service.decode()
 assert ' --run-once' in text and 'Type=oneshot' in text and 'TimeoutStartSec=7300' in text
 assert 'OnUnitInactiveSec=30min' in timer.decode()
 assert 'EnvironmentFile' not in text and 'YOUTUBE_API_KEY' not in text and 'worker' not in text.lower().split('execstart=')[0]


def run_fixture(value):
 calls=[];writes=[]
 host=SimpleNamespace(run=lambda command,**kw:(calls.append((command,kw)) or json.dumps(value)))
 operator=SimpleNamespace(install_file=lambda d,file,raw,**kw:writes.append((file,json.loads(raw),kw)))
 return host,operator,calls,writes


def response():
 return {'version':'completed-video-retention-refresh-v1','authorization_id':'owned-auth','apply':True,'max_jobs':1,
  'processed':[{'result':{'local_files_deleted':3}}], 'retained':[]}


def test_run_once_can_only_exec_lookup_maintenance_with_bound_authorization_and_bounded_timeout():
 host,operator,calls,writes=run_fixture(response());r=m.maintenance_once(host,operator,'owned-container','owned-auth')
 assert r['state']=='completed' and r['local_files_deleted']==3
 argv,kw=calls[0];assert argv[:6]==['/usr/bin/docker','exec','--user','10001:10001','owned-container','python']
 assert argv[6:]==['-m','services.hub.video_retention_refresh_v1','--authorization-id','owned-auth','--max-jobs','1','--apply']
 assert kw=={'timeout':7200} and len(writes)==2 and writes[-1][1]==r
 assert 'run-history' in str(writes[0][0]) and writes[1][0].name=='last-run.json'


@pytest.mark.parametrize('change',['wrong_owner','not_apply','too_many_files'])
def test_unbound_or_malformed_result_is_persisted_unknown_and_cannot_enable_timer(change):
 value=response()
 if change=='wrong_owner':value['authorization_id']='different'
 elif change=='not_apply':value['apply']=False
 else:value['processed'][0]['result']['local_files_deleted']=4
 host,operator,calls,writes=run_fixture(value)
 with pytest.raises(RuntimeError,match='RESULT_UNKNOWN'):m.maintenance_once(host,operator,'owned-container','owned-auth')
 assert len(writes)==2 and writes[-1][1]['state']=='unknown' and writes[-1][1]['local_files_deleted']==0
 assert writes[-1][1]['error_class']=='AssertionError' and len(calls)==1


def test_timeout_is_persisted_unknown_with_no_blind_second_command():
 import subprocess
 host,operator,calls,writes=run_fixture(response())
 def timeout(command,**kwargs):calls.append(command);raise subprocess.TimeoutExpired('owned-native-maintenance',7200,output=b'owned private diagnostics')
 host.run=timeout
 with pytest.raises(RuntimeError,match='RESULT_UNKNOWN'):m.maintenance_once(host,operator,'owned-container','owned-auth')
 assert len(calls)==1 and writes[-1][1]['state']=='unknown'
 assert 'owned private diagnostics' not in json.dumps(writes[-1][1])


def test_manual_pass_boolean_cannot_replace_the_70_actual_receipt(tmp_path):
 path=tmp_path/str(uuid.uuid4());path.mkdir();raw=json.dumps({'passed':True}).encode();(path/'test-attestation.json').write_bytes(raw)
 with pytest.raises((AssertionError,KeyError)):m.gate(path,m.digest(raw),SimpleNamespace(protected=lambda p:None))


def test_post_start_verification_failure_persists_reconciliation_without_enabling_timer(tmp_path):
 writes=[];report={'state':'installed_timer_not_enabled','timer_enabled':False}
 host=SimpleNamespace()
 operator=SimpleNamespace(core_processes=lambda d:{'unexpected':'core-changed'},
  install_file=lambda d,file,raw,**kw:writes.append((file,json.loads(raw))))
 with pytest.raises(RuntimeError,match='POST_START_REQUIRES_RECONCILIATION'):
  m.post_start_acceptance(host,operator,'owned-container',{'owned':'original-cores'},None,report,tmp_path)
 assert writes==[(tmp_path/'result.json',report)]
 assert report['state']=='requires_reconciliation' and report['error_class']=='AssertionError'
 assert report['timer_enabled'] is False and report['automatic_rollback_performed'] is False


def test_only_two_safe_original_byte_proof_files_are_bound_not_whole_evidence_directory(tmp_path):
 mounts=m.proof_mounts(helper(),tmp_path/'original-private-proof')
 assert len(mounts)==2 and all(v['read_only'] is True for v in mounts)
 assert {Path(v['source']).name for v in mounts}=={'test-attestation.json','accepted-results.xml'}
 assert {v['target'] for v in mounts}=={str(tmp_path/'original-private-proof'/name) for name in ('test-attestation.json','accepted-results.xml')}
 assert all(Path(v['source']).parent==m.BASE/'proof' for v in mounts)


def test_unreadable_or_invalid_app_release_after_start_is_recorded_before_timer_can_enable(tmp_path):
 writes=[];report={'state':'installed_timer_not_enabled','timer_enabled':False}
 operator=SimpleNamespace(core_processes=lambda d:{'owned':'same'},
  install_file=lambda d,file,raw,**kw:writes.append(json.loads(raw)))
 def unreadable():raise AssertionError('Owned missing proof mount, not real credentials')
 with pytest.raises(RuntimeError,match='POST_START_REQUIRES_RECONCILIATION'):
  m.post_start_acceptance(SimpleNamespace(),operator,'owned-container',{'owned':'same'},None,report,tmp_path,release_check=unreadable)
 assert writes[-1]['state']=='requires_reconciliation' and writes[-1]['timer_enabled'] is False
 assert 'Owned missing proof mount' not in json.dumps(writes[-1])
