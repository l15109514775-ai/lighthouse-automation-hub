"""Owned pure cutover and real SQLite boundaries; no Docker/SSH or proof claim."""
from copy import deepcopy
from pathlib import Path
import importlib.util,sqlite3
import pytest
from test_deploy_replay_contention42_v1 import compose as old_compose,runtime as old_runtime

file=Path(__file__).resolve().parents[2]/'scripts/ops/deploy_replay_manager_links41_v5.py'
spec=importlib.util.spec_from_file_location('owned_manager_cutover',file);op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

def compose():
    value=old_compose();value['services']['video-worker']['image']=op.OLD_IMAGE
    value['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    value['services']['video-worker']['tmpfs']=['/owned-unchanged-home:rw,size=8m']
    return value

def test_candidate_only_changes_image_and_readonly_proof_all_peers_and_flags_preserved():
    before=compose();after=op.candidate(before,op.PROOF.parent);reverse=deepcopy(after)
    reverse['services']['video-worker']['image']=op.OLD_IMAGE
    reverse['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    assert reverse==before and after['services']['video-worker']['tmpfs']==before['services']['video-worker']['tmpfs']
    assert all(after['services'][n]==before['services'][n] for n in ('api','scheduler','system-worker','tweet-worker'))

def test_wrong_or_writable_proof_never_changes_candidate():
    for change in ('old','writable','new'):
        before=compose();target=op.PROOF.parent
        if change=='old':before['services']['video-worker']['volumes'][0]['source']='/owned/other'
        elif change=='writable':before['services']['video-worker']['volumes'][0]['read_only']=False
        else:target=Path('/owned/other')
        with pytest.raises(ValueError):op.candidate(before,target)

def test_only_the_three_actual_git_source_pins_may_change_in_release():
    stable='services/hub/replay_parts_worker_runtime_v5.py'
    before={'test_attestation':{'sha256':op.OLD_PROOF_SHA},'authorization_sha256':op.OWNER_SHA,
        'source_files':{stable:'f'*64,**{n:'0'*64 for n in op.SOURCES}}}
    after={'source_files':{stable:'f'*64,**op.SOURCES}}
    op.validate_release_change(before,after)
    changed=deepcopy(after);changed['source_files'][stable]='e'*64
    with pytest.raises(ValueError,match='ONLY_THREE'):op.validate_release_change(before,changed)
    changed=deepcopy(after);changed['source_files'][next(iter(op.SOURCES))]='d'*64
    with pytest.raises(ValueError,match='ONLY_THREE'):op.validate_release_change(before,changed)

def test_runtime_env_order_and_derived_hostname_are_accepted_resource_or_scope_change_is_not():
    old=old_runtime();old['Image']=op.OLD_IMAGE;old['Mounts'][0]['Source']=str(op.OLD_PROOF)
    new=deepcopy(old);new['Id']='b'*64;new['Image']=op.IMAGE;new['Config']['Hostname']='b'*12
    new['Config']['Env'].reverse();new['Mounts'][0]['Source']=str(op.PROOF.parent)
    op.verify_runtime(old,new,{})
    changed=deepcopy(new);changed['Config']['Env'].append('EXTRA=denied')
    with pytest.raises(ValueError,match='ENV_CHANGED'):op.verify_runtime(old,changed,{})
    changed=deepcopy(new);changed['HostConfig']['PidsLimit']+=1
    with pytest.raises(ValueError,match='HOST_LIMITS_CHANGED'):op.verify_runtime(old,changed,{})

def test_paused_unknown_job_snapshot_readonly_never_resets_attempt_and_active_lease_blocks(runtime,tmp_path):
    settings,store=runtime;ident=store.enqueue('youtube','owned-source','owned-kind','video',{},'owned-policy')
    with store.transaction() as db:
        db.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
        db.execute("UPDATE jobs SET state='publish_unknown',attempts=2 WHERE job_id=?",(ident,))
    file=tmp_path/'snapshot.sqlite3';source=store.connect();target=sqlite3.connect(file)
    try:source.backup(target)
    finally:target.close();source.close()
    before=file.read_bytes();assert len(op.paused_snapshot(file))==64 and file.read_bytes()==before
    with sqlite3.connect(file) as db:
        assert db.execute('SELECT attempts FROM jobs').fetchall()==[(2,)]
        db.execute('UPDATE jobs SET lease_owner=?',(str('owned-live'),))
    db.close()  # The immutable snapshot is read only after its writer/checkpoint closes.
    with pytest.raises(ValueError,match='ACTIVE_VIDEO_REFUSED'):op.paused_snapshot(file)

def test_paused_acceptance_never_requires_retention_or_permits_live_browser():
    first={n:110.0 for n in op.REQUIRED_BEATS};later={n:111.0 for n in op.REQUIRED_BEATS}
    assert op.advancing(later,100.0,first,112.0)
    assert not op.advancing(first,100.0,first,112.0)
    assert 'worker:video-retention-v5' not in op.REQUIRED_BEATS
    namespace={};exec(compile(op.APP,'owned-probe','exec'),namespace)
    allowed=[(1,['/sbin/docker-init']),(9,['python','-m','services.hub.replay_parts_worker_runtime_v5','--publish'])]
    namespace['process_idle'](allowed+[(99,['python','-c','owned probe'])],99)
    with pytest.raises(ValueError,match='VIDEO_CHILD_PROCESS_STILL_ACTIVE'):
        namespace['process_idle'](allowed+[(10,['chromium','owned profile'])],99)
