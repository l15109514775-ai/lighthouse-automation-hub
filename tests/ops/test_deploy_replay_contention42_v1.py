"""Owned pure boundaries and real SQLite pause/lease checks; no Docker/SSH."""
from copy import deepcopy
from pathlib import Path
import importlib.util
import sqlite3

import pytest

file=Path(__file__).resolve().parents[2]/'scripts/ops/deploy_replay_contention42_v1.py'
spec=importlib.util.spec_from_file_location('owned_contention_cutover',file)
op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)


def compose():
    return {'services':{'video-worker':{'image':op.OLD_IMAGE,'environment':{'OWNED':'same'},
        'volumes':[{'target':'/run/qualifications/retention-v5','source':str(op.OLD_PROOF),
            'type':'bind','read_only':True,'bind':{'create_host_path':False}},
            {'target':'/owned','source':'/owned','read_only':False}]},
        **{n:{'image':'owned-peer','environment':{'PRESERVED':'value'}} for n in ('api','scheduler','tweet-worker','system-worker')}}}


def runtime():
    return {'Id':'a'*64,'Image':op.OLD_IMAGE,'State':{'Running':True,'Pid':123,'StartedAt':'2026-11-02T14:24:36Z'},
        'Config':{'Env':['A=one','B=two'],'Cmd':['python','-m','services.hub.replay_parts_worker_runtime_v5','--publish'],
            'User':'10001:10001','Entrypoint':[],'WorkingDir':'/opt/lighthouse-hub','Hostname':'a'*12},
        'HostConfig':{'Binds':['old-proof'],'Memory':1400*1024**2,'ReadonlyRootfs':True,'PidsLimit':256},
        'Mounts':[{'Type':'bind','Destination':'/run/qualifications/retention-v5','Source':str(op.OLD_PROOF),'RW':False},
            {'Type':'bind','Destination':'/owned','Source':'/owned','RW':True}]}


def test_candidate_only_changes_video_image_and_exact_proof():
    before=compose();after=op.candidate(before,op.PROOF.parent)
    restored=deepcopy(after);restored['services']['video-worker']['image']=op.OLD_IMAGE
    restored['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    assert restored==before and after['services']['tweet-worker']==before['services']['tweet-worker']
    assert before['services']['video-worker']['image']==op.OLD_IMAGE


@pytest.mark.parametrize('change',['wrong_old_proof','writable_proof','wrong_new_proof'])
def test_candidate_refuses_unqualified_or_writable_proof(change):
    before=compose();target=op.PROOF.parent
    if change=='wrong_old_proof':before['services']['video-worker']['volumes'][0]['source']='/owned/other'
    elif change=='writable_proof':before['services']['video-worker']['volumes'][0]['read_only']=False
    else:target=Path('/owned/other-new-proof')
    with pytest.raises(ValueError):op.candidate(before,target)


def test_runtime_accepts_env_order_and_derived_hostname_only():
    old=runtime();new=deepcopy(old);new['Id']='b'*64;new['Image']=op.IMAGE
    new['Config']['Hostname']='b'*12;new['Config']['Env'].reverse()
    new['HostConfig']['Binds']=['new-proof'];new['Mounts'][0]['Source']=str(op.PROOF.parent)
    op.verify_runtime(old,new,{})
    new['Config']['Env'].append('A=one')
    with pytest.raises(ValueError,match='DUPLICATE_ENV'):op.verify_runtime(old,new,{})


@pytest.mark.parametrize('change',['limit','env_value','other_mount'])
def test_runtime_rejects_resource_environment_or_other_mount_changes(change):
    old=runtime();new=deepcopy(old);new['Image']=op.IMAGE;new['Mounts'][0]['Source']=str(op.PROOF.parent)
    if change=='limit':new['HostConfig']['PidsLimit']+=1
    elif change=='env_value':new['Config']['Env'][0]='A=changed'
    else:new['Mounts'][1]['RW']=False
    with pytest.raises(ValueError):op.verify_runtime(old,new,{})


def snapshot(runtime,tmp_path,*,paused,state='awaiting_review',owner=None):
    settings,store=runtime
    job=store.enqueue('youtube','owned-video','owned-kind','video',{'owned':True},'owned-policy')
    with store.transaction() as db:
        db.execute("UPDATE pipelines SET paused=? WHERE name='video'",(paused,))
        db.execute('UPDATE jobs SET state=?,attempts=1,lease_owner=?,lease_until=? WHERE job_id=?',
            (state,owner,123456789.0 if owner else None,job))
    target=tmp_path/'snapshot.sqlite3'
    live=store.connect();copy=sqlite3.connect(target)
    try:live.backup(copy)
    finally:copy.close();live.close()
    return target


def test_real_idle_paused_snapshot_is_readonly_and_preserves_attempts(runtime,tmp_path):
    target=snapshot(runtime,tmp_path,paused=1);before=target.read_bytes()
    assert len(op.paused_snapshot(target))==64 and target.read_bytes()==before
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT attempts FROM jobs').fetchall()==[(1,)]


@pytest.mark.parametrize('state,owner',[('processing',None),('processing','owned-lease'),('awaiting_review','owned-lease')])
def test_real_snapshot_refuses_any_active_or_stranded_lease(runtime,tmp_path,state,owner):
    target=snapshot(runtime,tmp_path,paused=1,state=state,owner=owner);before=target.read_bytes()
    with pytest.raises(ValueError,match='ACTIVE_VIDEO_REFUSED'):op.paused_snapshot(target)
    assert target.read_bytes()==before


def test_real_snapshot_requires_existing_maintenance_pause(runtime,tmp_path):
    target=snapshot(runtime,tmp_path,paused=0)
    with pytest.raises(ValueError,match='MAINTENANCE_PAUSE_REQUIRED'):op.paused_snapshot(target)


def test_paused_heartbeats_do_not_require_or_fabricate_retention():
    first={name:110.0 for name in op.REQUIRED_BEATS};nextbeats={name:111.0 for name in op.REQUIRED_BEATS}
    assert op.advancing(nextbeats,100.0,first,112.0)
    assert not op.advancing(first,100.0,first,112.0)
    assert not op.advancing(nextbeats,100.0,None,112.0)
    assert not op.advancing({name:99.0 for name in op.REQUIRED_BEATS},100.0,first,112.0)


@pytest.mark.parametrize('child',['ffprobe','chromium'])
def test_live_child_process_refuses_idle_without_killing(child):
    namespace={};exec(compile(op.APP,'owned-idle-probe','exec'),namespace)
    allowed=[(1,['/sbin/docker-init']),(8,['python','-m','services.hub.replay_parts_worker_runtime_v5','--publish'])]
    namespace['process_idle'](allowed+[(99,['python','-c','owned probe'])],99)
    with pytest.raises(ValueError,match='VIDEO_CHILD_PROCESS_STILL_ACTIVE'):
        namespace['process_idle'](allowed+[(9,[child,'owned media'])],99)
