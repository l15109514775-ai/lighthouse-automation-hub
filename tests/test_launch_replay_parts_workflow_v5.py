"""Host launch boundaries and real owned completion -> deletion -> launch.

The FFmpeg/public DOM fixture is owned evidence, not a live Bilibili result.
Only root-file admission is an explicit test seam. No Docker/provider/SSH runs.
"""
from copy import deepcopy
from datetime import datetime,timezone
import importlib.util
import json,sqlite3,time
from pathlib import Path
from types import SimpleNamespace
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_parts_v5 import ReplayPartGroupsV5,ReplayPartVideoWeb
from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5,MARKER
from test_replay_parts_retention_v5 import completed
from test_replay_parts_v5 import reserved,publisher
from test_replay_delivery_v5 import delivery
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks
from test_youtube_download_v4 import toml

_path=Path(__file__).resolve().parents[1]/'scripts/ops/launch_replay_parts_workflow_v5.py'
_spec=importlib.util.spec_from_file_location('owned_formal_launch',_path)
op=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(op)


def policy_raw(**change):
    policy={'version':'replay-part-video-policy-v5','enabled':True,'source_scope':'pilot_single',
        'acceptance_video_ids':[op.PILOT],'automation_enabled':False,'target_site_uid':'1000000000000000',
        'source_not_before':'2026-10-31T16:00:00Z','activated_at':'2026-11-02T12:04:32Z',
        'allowed_channel_ids':['owned-channel-a','owned-channel-b'],
        'actual_owner_workflow_response':'Owned test quote; not live owner authorization',
        'owner_response_reference':'owned-chat-reference','workflow_authorization_reference':'owned-workflow-reference',
        **change}
    return ('\n'.join(k+' = '+('true' if v is True else 'false' if v is False else json.dumps(v)) for k,v in policy.items())+'\n').encode()


def compose():
    return {'services':{'video-worker':{'image':op.IMAGE,'command':op.ENTRY,'user':'10001:10001',
        'hostname':'lighthouse-video-worker','environment':{'HUB_CONFIG':'/owned/config'},
        'volumes':[{'type':'bind','source':'/owned/data','target':'/owned/data','read_only':False}],
        'tmpfs':['/tmp:rw,size=192m'],'mem_limit':'1400m'},
        **{name:{'image':'owned-other','environment':{'UNCHANGED':'true'}} for name in ('api','scheduler','tweet-worker','system-worker')}}}


def runtime():
    return {'Id':'a'*64,'Image':op.IMAGE,'State':{'Pid':41,'StartedAt':'2026-11-02T12:04:32Z','Running':True},
        'Config':{'Env':['FIRST=one','SECOND=two'],'Cmd':op.ENTRY,'Entrypoint':[],'User':'10001:10001',
            'WorkingDir':'/opt/lighthouse-hub','Hostname':'lighthouse-video-worker'},
        'HostConfig':{'Binds':['owned bind'],'Memory':1400000000,'PidsLimit':256,'RestartPolicy':{'Name':'unless-stopped'}},
        'Mounts':[{'Destination':'/owned/data','Source':'/owned/data','Type':'bind','RW':True}]}


def test_policy_changes_exactly_scope_and_automation_preserving_cutoff_and_owner():
    raw=policy_raw();old=op.tomllib.loads(raw.decode());new,changed=op.formal_policy(raw)
    assert {k for k in old if old[k]!=new[k]}=={'source_scope','acceptance_video_ids','automation_enabled'}
    assert new['source_not_before']==old['source_not_before'] and new['activated_at']==old['activated_at']
    assert op.tomllib.loads(changed.decode())==new and new['actual_owner_workflow_response']==old['actual_owner_workflow_response']


@pytest.mark.parametrize('change',[{'acceptance_video_ids':['9T5tn1WGlJg']},{'automation_enabled':True},{'source_scope':'all_eligible_ended_since_operating_cutoff'}])
def test_only_current_pilot_can_be_switched(change):
    with pytest.raises(ValueError,match='EXACT_EXISTING_PILOT'):op.formal_policy(policy_raw(**change))


def test_compose_only_one_root_grant_mount_and_other_four_exactly_unchanged():
    old=compose();new=op.candidate(old);mount=new['services']['video-worker']['volumes'].pop()
    assert new==old and mount=={'type':'bind','source':str(op.GRANT),'target':str(op.GRANT),
        'read_only':True,'bind':{'create_host_path':False}}
    old['services']['video-worker']['volumes'].append(mount)
    with pytest.raises(ValueError,match='ALREADY_MOUNTED'):op.candidate(old)


def test_runtime_verification_allows_env_order_but_rejects_mount_or_resource_drift():
    old=runtime();new=deepcopy(old);new['Id']='b'*64;new['Config']['Env'].reverse()
    new['HostConfig']['Binds'].append('new root grant')
    new['Mounts'].append({'Destination':str(op.GRANT),'Source':str(op.GRANT),'Type':'bind','RW':False})
    op.verify_running(old,new,compose())
    new['Mounts'][-1]['RW']=True
    with pytest.raises(ValueError,match='ROOT_GRANT'):op.verify_running(old,new,compose())
    new['Mounts'][-1]['RW']=False;new['HostConfig']['Memory']+=1
    with pytest.raises(ValueError,match='HOST_LIMITS'):op.verify_running(old,new,compose())


def test_plan_binds_real_prior_references_not_new_approval():
    old=op.tomllib.loads(policy_raw().decode())
    acceptance={'source_id':op.PILOT,'target_site_uid':old['target_site_uid'],'acceptance_parent_job_id':'owned-parent',
        'owner_references':{k:old[k] for k in ('actual_owner_workflow_response','owner_response_reference','workflow_authorization_reference')},
        'completed_group_result_sha256':'e'*64,'retention':{'receipt_sha256':'f'*64},'idle':{'volatile':True}}
    args=dict(accepted_at=datetime.now(timezone.utc).isoformat(),acceptance=acceptance,policy_raw=policy_raw(),
        compose=op.wire(compose()),video=runtime(),peers={'api':{'id':'unchanged'}},config_hashes={'hub.toml':'c'*64},
        release={'source_files':{'owned':'d'*64}},proof_sha=op.PROOF_SHA,operator_sha='a'*64)
    plan,new_raw=op.make_plan(**args)
    assert plan['plan_sha256']==op.digest({k:v for k,v in plan.items() if k!='plan_sha256'})
    assert plan['grant']['policy_sha256']==op.sha(new_raw) and plan['new_owner_approval_claimed'] is False
    assert plan['grant']['completed_group_result_sha256']=='e'*64 and 'idle' not in plan['acceptance']
    assert plan['grant']['actual_owner_workflow_response']==old['actual_owner_workflow_response']
    acceptance['owner_references'].pop('owner_response_reference')
    with pytest.raises(ValueError,match='OWNER_OR_TARGET'):op.make_plan(**args)


def qualification(monkeypatch):
    xml=('<testsuite>'+''.join('<testcase classname="tests.test_replay_parts_retention_v5" name="owned%d"/>'%i for i in range(26))+'</testsuite>').encode()
    proof={'version':'replay-parts-retention-v5-ubuntu-tests','image':op.IMAGE,'tests':26,
        'test_groups':{'tests.test_replay_parts_retention_v5':26},'junit_sha256':op.sha(xml),'network':'none',
        'source_files':{f'owned-source-{i}':'a'*64 for i in range(141)},
        **{k:0 for k in ('exit_code','failures','errors','skipped')},
        **{k:True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','mounted_sources_unchanged')},
        **{k:False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed','timed_out')}}
    raw=op.wire(proof);monkeypatch.setattr(op,'PROOF_SHA',op.sha(raw))  # Named owned root-proof/raw pin seam.
    return proof,raw,xml,{'test_attestation':{'sha256':op.sha(raw)},'source_files':deepcopy(proof['source_files'])}


def test_qualification_requires_raw_junit_source_map_and_no_runtime_denial(monkeypatch):
    proof,raw,xml,release=qualification(monkeypatch);op.validate_qualification(proof,raw,xml,release)
    with pytest.raises(ValueError):op.validate_qualification(proof,raw+b' ',xml,release)
    with pytest.raises(ValueError):op.validate_qualification(proof,raw,xml.replace(b'testcase',b'skipped',1),release)
    changed=deepcopy(release);changed['source_files']['owned-source-0']='b'*64
    with pytest.raises(ValueError,match='SOURCE_PROOF'):op.validate_qualification(proof,raw,xml,changed)
    proof['no_pid_or_oom_denial']=False
    with pytest.raises(ValueError,match='NOT_ISOLATED'):op.validate_qualification(proof,raw,xml,release)
    # Even an owned raw-hash admission seam cannot qualify the older image.
    wrong=deepcopy(proof);wrong['no_pid_or_oom_denial']=True
    wrong['image']='sha256:24b0198ba425f527655173c2ef1432445947ebec5fed8660c6bb5527eaa15dde'
    oldraw=op.wire(wrong);monkeypatch.setattr(op,'PROOF_SHA',op.sha(oldraw))
    oldrelease=deepcopy(release);oldrelease['test_attestation']['sha256']=op.sha(oldraw)
    with pytest.raises(ValueError,match='JUNIT_OR_IMAGE'):op.validate_qualification(wrong,oldraw,xml,oldrelease)


def app():
    namespace={};exec(compile(op.APP,'owned-formal-app','exec'),namespace);return SimpleNamespace(**namespace)


def owned_after_cleanup(f,monkeypatch):
    plan=f.retention.plan(f.parent);receipt=f.retention.apply(plan)
    assert receipt['local_files_deleted']>0 and all(not Path(v['file']).exists() for v in receipt['deleted_candidates'])
    # The existing 26 retention tests qualify root admission. This owned test
    # supplies that one explicit root gate, leaving all evidence/CAS/unlinks real.
    monkeypatch.setattr(ReplayPartsRetentionV5,'_release',lambda self:(self._authorization(f.owner['authorization_id']),'owned-root-release-only'))
    return receipt


def test_real_deleted_pilot_actual_ledger_and_both_markers_then_actual_require_launch(completed,monkeypatch):
    f=completed;owned_after_cleanup(f,monkeypatch);probe=app();store=probe.ReadOnlyStore(f.s.db_path)
    try:
        actual=probe.collect_acceptance(f.s,store,f.parent,expected_source=f.raw['id'])
        assert actual['part_count']==1 and actual['retention']['completed_audit_present'] is True
        assert actual['database_rows_changed']==0 and actual['public_parts'][0]['external_id']=='BV1owned0001'
        formal={**f.publish_policy,'source_scope':'all_eligible_ended_since_operating_cutoff','acceptance_video_ids':[],'automation_enabled':True}
        (f.s.root/'config'/f.publish.POLICY_FILE).write_text(toml(formal),'utf-8')
        policy=f.publish.policy()
        # Only protected root-file bytes are replaced with the owned grant. The
        # real require_launch below reads completed jobs, immutable first ref,
        # actual public refs, original publisher audit and real SQLite registry.
        grant={'version':'replay-parts-workflow-launch-v5','policy_sha256':policy['_sha256'],
            'target_site_uid':policy['target_site_uid'],'workflow_authorization_reference':policy['workflow_authorization_reference'],
            'actual_owner_workflow_response':policy['actual_owner_workflow_response'],'owner_response_reference':policy['owner_response_reference'],
            'accepted_at':datetime.now(timezone.utc).isoformat(),'acceptance_parent_job_id':f.parent,
            'completed_group_result_sha256':actual['completed_group_result_sha256']}
        monkeypatch.setattr(ReplayPartVideoWeb,'_root_readonly_file',lambda self,path,maximum:op.wire(grant))
        assert ReplayPartGroupsV5(f.s,store).require_launch(policy)==grant
        assert all(Path(r['file']).is_file() for r in actual['required_public_references'])
    finally:store.db.close()


@pytest.mark.parametrize('change',['hub_marker','operation_pending','audit_missing','original_public_ref_changed'])
def test_missing_real_completion_facts_cannot_become_launch_grant(completed,monkeypatch,change):
    f=completed;owned_after_cleanup(f,monkeypatch)
    if change=='operation_pending':
        with sqlite3.connect(f.retention.index) as db:db.execute("UPDATE operations SET state='deleting'")
    elif change=='original_public_ref_changed':
        first=json.loads(Path(f.db.get(f.parent)['result']['replay_parts_first_completion_v5']['file']).read_bytes())
        Path(first['parts'][0]['public_coverage']['manager_observation']['file']).write_bytes(b'{}')
    else:
        with f.db.transaction() as db:
            if change=='hub_marker':
                source=f.db.get(f.job['job_id']);source['result'].pop(MARKER)
                db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),source['job_id']))
            else:db.execute('DELETE FROM audit WHERE action=?',('replay-parts-local-media-retention-v5',))
    probe=app();store=probe.ReadOnlyStore(f.s.db_path)
    try:
        with pytest.raises((ValueError,HubError)):probe.collect_acceptance(f.s,store,f.parent,expected_source=f.raw['id'])
        assert store.db.total_changes==0
    finally:store.db.close()


def test_readonly_idle_probe_refuses_even_a_pending_job_with_live_lease(completed):
    f=completed;probe=app();row=f.db.get(f.child)
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending',lease_owner='owned-owner',lease_until=? WHERE job_id=?",(time.time()+120,row['job_id']))
    store=probe.ReadOnlyStore(f.s.db_path)
    try:
        with pytest.raises(ValueError,match='ACTIVE_VIDEO_OR_LEASE'):probe.idle(store)
        assert store.db.total_changes==0
    finally:store.db.close()


def test_idle_process_probe_refuses_download_or_browser_subprocess_without_killing(tmp_path):
    namespace={};exec(compile(op.APP,'owned-process-probe','exec'),namespace)
    proc=tmp_path/'proc';proc.mkdir()
    for pid,args in ((1,b'/sbin/docker-init\0'),(18,b'python\0-m\0services.hub.replay_parts_worker_runtime_v5\0--publish\0')):
        row=proc/str(pid);row.mkdir();(row/'cmdline').write_bytes(args)
    namespace['Path']=lambda value:proc if value=='/proc' else Path(value)
    store=SimpleNamespace(query=lambda sql:[])
    assert namespace['idle'](store,check_processes=True)['processes_checked'] is True
    row=proc/'19';row.mkdir();(row/'cmdline').write_bytes(b'/usr/bin/ffmpeg\0-owned-test\0')
    with pytest.raises(ValueError,match='SUBPROCESS_OR_PROFILE_OWNER'):namespace['idle'](store,check_processes=True)
    assert (row/'cmdline').is_file()  # Observation never sends a signal/kills a process.


def test_preflight_has_only_snapshot_small_refs_ro_no_profile_or_provider_key(tmp_path,monkeypatch):
    root=tmp_path/'root';file=root/'media'/'owned'/'proof.json';file.parent.mkdir(parents=True);file.write_bytes(b'{}')
    monkeypatch.setattr(op,'ROOT',root);calls=[]
    expected={'claims':0,'external_requests':0,'live_database_writes':0,'handlers':36,'retention_release_valid':True}
    def run(args,**kw):calls.append((args,kw));return SimpleNamespace(stdout=op.wire(expected))
    helper=SimpleNamespace(run=run,PRECHECK='owned-precheck')
    env={k:'owned-value' for k in ('DRY_RUN','HOME','HUB_CONFIG','HUB_ROOT','PYTHONDONTWRITEBYTECODE','XDG_CACHE_HOME','XDG_CONFIG_HOME','YOUTUBE_API_KEY_FILE')}
    result=op.preflight(helper,op.IMAGE,tmp_path/'stage',tmp_path/'proof',env,[{'file':str(file),'bytes':2,'sha256':op.sha(b'{}')}])
    assert result==expected and len(calls)==1;args=calls[0][0]
    assert '--network=none' in args and '--read-only' in args and '--pids-limit=256' in args
    binds=[v for v in args if v.startswith('--mount=')]
    assert len(binds)==5 and all(v.endswith(',readonly') for v in binds)
    assert not any('browser-profiles' in v or '/run/secrets/' in v for v in binds)
    assert any('target='+str(op.GRANT) in v for v in binds)
    env['MODEL_API_KEY']='owned-secret-value'
    with pytest.raises(ValueError,match='NARROW_PREFLIGHT'):op.preflight(helper,op.IMAGE,tmp_path/'stage',tmp_path/'proof',env,[])
    assert len(calls)==1
