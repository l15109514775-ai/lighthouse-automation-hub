"""Owned pure/SQLite cutover boundaries; no Docker, SSH or live qualification."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,sqlite3,stat
import pytest
from test_deploy_replay_contention42_v1 import compose as original_compose,runtime as original_runtime

FILE=Path(__file__).resolve().parents[2]/'scripts/ops/deploy_two_replay_repair_v6.py'
spec=importlib.util.spec_from_file_location('owned_two_replay_cutover',FILE)
op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)
SCOPE={'adapters/ytdlp_highest_v2.py','adapters/replay_later_result_lookup_v2.mjs',
       'services/hub/replay_part_moderation_v5.py','adapters/replay_native_driver.mjs'}

@pytest.fixture
def candidate_values(monkeypatch):
    # These pins are explicitly owned seams, not actual qualification proof.
    monkeypatch.setattr(op,'IMAGE','sha256:'+'b'*64)
    monkeypatch.setattr(op,'PROOF',Path('/owned/new26/test-attestation.json'))
    monkeypatch.setattr(op,'SOURCES',{n:'c'*64 for n in SCOPE})

def runtimes():
    old=original_runtime();old['Image']=op.OLD_IMAGE;old['Mounts'][0]['Source']=str(op.OLD_PROOF)
    old['HostConfig'].update(SecurityOpt=['no-new-privileges:true','seccomp={"defaultAction":"SCMP_ACT_ERRNO","architectures":["SCMP_ARCH_X86_64"]}'],CapDrop=['NET_RAW','SYS_ADMIN'],Dns=['192.0.2.1','192.0.2.2'])
    old['Mounts'].append({'Type':'bind','Source':str(op.GRANT),'Destination':str(op.GRANT),'RW':False})
    new=deepcopy(old);new['Id']='b'*64;new['Image']=op.IMAGE;new['Config']['Hostname']='b'*12
    new['Mounts'][0]['Source']=str(op.PROOF.parent)
    return old,new

def test_only_video_image_and_proof_change_preserves_formal_grant_and_four_peers(candidate_values):
    before=original_compose();service=before['services']['video-worker'];service['image']=op.OLD_IMAGE
    service['volumes'][0]['source']=str(op.OLD_PROOF)
    grant={'target':str(op.GRANT),'source':str(op.GRANT),'type':'bind','read_only':True,'bind':{'create_host_path':False}}
    service['volumes'].append(grant)
    after=op.candidate(before,op.PROOF.parent);reverse=deepcopy(after)
    reverse['services']['video-worker']['image']=op.OLD_IMAGE
    reverse['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    assert reverse==before and after['services']['video-worker']['volumes'][-1]==grant
    assert all(after['services'][n]==before['services'][n] for n in ('api','scheduler','tweet-worker','system-worker'))

def test_unique_mount_order_and_unordered_security_sets_may_change_order_only(candidate_values):
    old,new=runtimes();new['Config']['Env'].reverse();new['Mounts'].reverse();new['HostConfig']['CapDrop'].reverse()
    new['HostConfig']['SecurityOpt']=['seccomp={"architectures":["SCMP_ARCH_X86_64"],"defaultAction":"SCMP_ACT_ERRNO"}','no-new-privileges:true']
    op.verify_runtime(old,new,{})

def test_empty_dns_serialization_is_equivalent_without_ignoring_nonempty_order(candidate_values):
    old,new=runtimes()
    for name in ('Dns','DnsOptions','DnsSearch'):old['HostConfig'][name]=[];new['HostConfig'][name]=None
    op.verify_runtime(old,new,{})
    old['HostConfig']['DnsSearch']=None;new['HostConfig']['DnsSearch']=[];op.verify_runtime(old,new,{})

@pytest.mark.parametrize('change,field',[
    ('resources','HostConfig.PidsLimit'),('security','HostConfig.SecurityOpt'),('dns_order','HostConfig.Dns'),
    ('command_order','Config.Cmd'),('grant_rw','Mounts.other'),('environment','Config.Env'),
])
def test_real_runtime_changes_fail_with_field_names_without_env_values(candidate_values,change,field,capsys):
    old,new=runtimes()
    if change=='resources':new['HostConfig']['PidsLimit']+=1
    elif change=='security':new['HostConfig']['SecurityOpt'][1]='seccomp={"defaultAction":"SCMP_ACT_ALLOW"}'
    elif change=='dns_order':new['HostConfig']['Dns'].reverse()
    elif change=='command_order':new['Config']['Cmd'].reverse()
    elif change=='environment':new['Config']['Env'].append('OWNED_SECRET=not-for-report')
    else:next(m for m in new['Mounts'] if m['Destination']==str(op.GRANT))['RW']=True
    with pytest.raises(ValueError,match='RUNTIME_CONFIGURATION_CHANGED') as error:op.verify_runtime(old,new,{})
    assert field in error.value.differing_fields and not capsys.readouterr().out
    assert 'not-for-report' not in str(error.value)

def test_duplicate_mount_destinations_do_not_disappear_during_normalization(candidate_values):
    old,new=runtimes();new['Mounts'].append(deepcopy(new['Mounts'][0]))
    with pytest.raises(ValueError,match='DUPLICATE_MOUNT_DESTINATION'):op.verify_runtime(old,new,{})

def test_full_inspect_is_root0600_and_reports_only_reference_hash(capsys):
    class RootFile:
        def __init__(self,name):self.name=name
        def __str__(self):return '/owned/root-evidence/'+self.name
        def stat(self):return SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o600)
    class Folder:
        def __truediv__(self,name):return RootFile(name)
    saved={}
    def write(file,raw,mode):saved.update(file=str(file),raw=raw,mode=mode)
    info={'Id':'owned','Config':{'Env':['OWNED_SECRET=fixture-only']},'HostConfig':{'PidsLimit':256}}
    proof=op.save_runtime_snapshot(SimpleNamespace(save=write,read=lambda file:saved['raw']),Folder(),info,'runtime-before.full.json')
    assert json.loads(saved['raw'])==info and saved['mode']==0o600
    assert proof['mode']=='0600' and proof['sha256']==op.sha(saved['raw'])
    assert 'OWNED_SECRET' not in json.dumps(proof) and not capsys.readouterr().out

def release_pair():
    prior={'test_attestation':{'sha256':op.OLD_PROOF_SHA},'authorization_sha256':op.OWNER_SHA,
        'source_files':{'services/hub/replay_parts_worker_runtime_v5.py':'f'*64,**{n:'0'*64 for n in SCOPE}}}
    current=deepcopy(prior)
    current['source_files'].update(op.SOURCES)
    return prior,current

def test_release_accepts_exact_four_changed_runtime_paths(candidate_values):
    prior,current=release_pair();op.validate_release_change(prior,current)
    assert {n for n in prior['source_files'] if prior['source_files'][n]!=current['source_files'][n]}==SCOPE

@pytest.mark.parametrize('change',['foreign_runtime','missing_repair','extra_path','different_owner','different_parent_proof'])
def test_release_rejects_scope_or_provenance_changes(candidate_values,change):
    prior,current=release_pair()
    if change=='foreign_runtime':current['source_files']['services/hub/replay_parts_worker_runtime_v5.py']='d'*64
    elif change=='missing_repair':current['source_files'][sorted(SCOPE)[0]]='0'*64
    elif change=='extra_path':current['source_files']['adapters/unqualified.py']='a'*64
    elif change=='different_owner':prior['authorization_sha256']='1'*64
    else:prior['test_attestation']['sha256']='2'*64
    with pytest.raises(ValueError,match='ONLY_QUALIFIED_REPAIR_SOURCES'):op.validate_release_change(prior,current)

def test_exact_current_parent_plus_one_copy_layer_keeps_full_image_configuration(candidate_values):
    config={'User':'10001:10001','Env':['OWNED=fixture'],'Cmd':['owned'],'Labels':{'owned':'fixture'}}
    original={'Id':op.OLD_IMAGE,'RootFS':{'Layers':['flat','workdir','a','b','runtime-alias']},'Config':deepcopy(config),'Os':'linux','Architecture':'amd64'}
    candidate={'Id':op.IMAGE,'RootFS':{'Layers':original['RootFS']['Layers']+['two-repair-copy']},'Config':deepcopy(config),'Os':'linux','Architecture':'amd64'}
    op.validate_image_lineage(original,candidate)
    for change in ('ancestry','config','platform','layer_count'):
        bad=deepcopy(candidate)
        if change=='ancestry':bad['RootFS']['Layers'][0]='foreign'
        elif change=='config':bad['Config']['Env'].append('INJECTED=refused')
        elif change=='platform':bad['Architecture']='arm64'
        else:bad['RootFS']['Layers'].append('extra-copy')
        with pytest.raises(ValueError):op.validate_image_lineage(original,bad)

def test_unfilled_qualification_constants_prevent_activation(monkeypatch):
    monkeypatch.setattr(op,'IMAGE','PENDING_ACTUAL_QUALIFIED_IMAGE')
    assert not op.activation_constants_ready()
    with pytest.raises(ValueError,match='ACTUAL_TWO_REPLAY_REPAIR_AND26_STILL_PENDING'):
        op.validate_qualifications(None,b'',b'',b'',b'',{})

@pytest.mark.parametrize('change',['none','unpaused','active_lease','processing'])
def test_readonly_snapshot_requires_video_pause_and_idle_jobs(tmp_path,change):
    file=tmp_path/'snapshot.sqlite3'
    with sqlite3.connect(file) as db:
        db.execute('CREATE TABLE pipelines(name TEXT,paused INTEGER)');db.execute("INSERT INTO pipelines VALUES('video',1)")
        db.execute('CREATE TABLE jobs(job_id TEXT,queue TEXT,state TEXT,lease_owner TEXT,lease_until REAL)')
        db.execute("INSERT INTO jobs VALUES('owned','video','awaiting_review',NULL,NULL)")
        if change=='unpaused':db.execute('UPDATE pipelines SET paused=0')
        elif change=='active_lease':db.execute('UPDATE jobs SET lease_until=99')
        elif change=='processing':db.execute("UPDATE jobs SET state='processing'")
    before=file.read_bytes()
    if change=='none':assert len(op.paused_snapshot(file))==64
    else:
        with pytest.raises(ValueError):op.paused_snapshot(file)
    assert file.read_bytes()==before

@pytest.mark.parametrize('change',['none','missing_role','same_heartbeat','pre_start','future'])
def test_new_video_requires_all_three_fresh_advancing_heartbeats(change):
    first={n:101 for n in op.REQUIRED_BEATS};beats={n:102 for n in op.REQUIRED_BEATS};name=next(iter(beats))
    if change=='missing_role':beats.pop(name)
    elif change=='same_heartbeat':beats[name]=101
    elif change=='pre_start':beats[name]=99
    elif change=='future':beats[name]=104
    assert op.advancing(beats,100,first,102) is (change=='none')

def test_current_video_and_parent70_are_pinned_to_actual_unchanged_parent():
    assert op.CURRENT=='7d01f1363649291076f79550af633040a4a3a33146120b143ee6329718d38a6e'
    assert op.OLD_IMAGE=='sha256:f9eb4449dc9c8e410935329e21ec147a55dc7ba80056db8f8eb27fd5551d82be'
    assert op.PARENT70_SHA=='309d2b2228b29f26f8edfb702b74d055e36076e1ffba12f7f15782edf8419f6a'
