"""Owned pure/SQLite deploy boundaries; no Docker, SSH, proof or activation."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,stat
import pytest
from test_deploy_replay_contention42_v1 import compose as original_compose,runtime as original_runtime

file=Path(__file__).resolve().parents[2]/'scripts/ops/deploy_replay_duration_submit_v5.py'
spec=importlib.util.spec_from_file_location('owned_duration_cutover',file)
op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

@pytest.fixture
def candidate_values(monkeypatch):
    # These explicit owned constants are not actual qualification evidence.
    monkeypatch.setattr(op,'IMAGE','sha256:'+'b'*64)
    monkeypatch.setattr(op,'PROOF',Path('/owned/new26/test-attestation.json'))

def runtimes():
    old=original_runtime();old['Image']=op.OLD_IMAGE;old['Mounts'][0]['Source']=str(op.OLD_PROOF)
    old['HostConfig'].update(SecurityOpt=['no-new-privileges:true','seccomp={"defaultAction":"SCMP_ACT_ERRNO","architectures":["SCMP_ARCH_X86_64"]}'],CapDrop=['NET_RAW','SYS_ADMIN'],Dns=['192.0.2.1','192.0.2.2'])
    old['Mounts'].append({'Type':'bind','Source':str(op.GRANT),'Destination':str(op.GRANT),'RW':False})
    new=deepcopy(old);new['Id']='b'*64;new['Image']=op.IMAGE;new['Config']['Hostname']='b'*12
    new['Mounts'][0]['Source']=str(op.PROOF.parent)
    return old,new

def test_only_image_proof_change_preserves_existing_formal_grant_and_peers(candidate_values):
    before=original_compose();service=before['services']['video-worker'];service['image']=op.OLD_IMAGE
    service['volumes'][0]['source']=str(op.OLD_PROOF)
    grant={'target':str(op.GRANT),'source':str(op.GRANT),'type':'bind','read_only':True,'bind':{'create_host_path':False}}
    service['volumes'].append(grant)
    after=op.candidate(before,op.PROOF.parent);reverse=deepcopy(after)
    reverse['services']['video-worker']['image']=op.OLD_IMAGE;reverse['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    assert reverse==before and after['services']['video-worker']['volumes'][-1]==grant
    assert all(after['services'][n]==before['services'][n] for n in ('api','scheduler','tweet-worker','system-worker'))

def test_only_nonsemantic_option_order_and_unique_mount_order_are_normalized(candidate_values):
    old,new=runtimes();new['Config']['Env'].reverse();new['Mounts'].reverse()
    new['HostConfig']['CapDrop'].reverse()
    new['HostConfig']['SecurityOpt']=['seccomp={"architectures":["SCMP_ARCH_X86_64"],"defaultAction":"SCMP_ACT_ERRNO"}','no-new-privileges:true']
    op.verify_runtime(old,new,{})

def test_unset_dns_null_and_empty_lists_are_equivalent_without_changing_nonempty_order(candidate_values):
    old,new=runtimes()
    for name in ('Dns','DnsOptions','DnsSearch'):old['HostConfig'][name]=[];new['HostConfig'][name]=None
    op.verify_runtime(old,new,{})
    old['HostConfig']['DnsSearch']=None;new['HostConfig']['DnsSearch']=[]
    op.verify_runtime(old,new,{})

@pytest.mark.parametrize('change,field',[
    ('resources','HostConfig.PidsLimit'),('security','HostConfig.SecurityOpt'),
    ('dns_order','HostConfig.Dns'),('command_order','Config.Cmd'),('grant_rw','Mounts.other'),
])
def test_true_changes_stop_with_exact_field_without_printing_environment(candidate_values,change,field,capsys):
    old,new=runtimes()
    if change=='resources':new['HostConfig']['PidsLimit']+=1
    elif change=='security':new['HostConfig']['SecurityOpt'][1]='seccomp={"defaultAction":"SCMP_ACT_ALLOW","architectures":["SCMP_ARCH_X86_64"]}'
    elif change=='dns_order':new['HostConfig']['Dns'].reverse()
    elif change=='command_order':new['Config']['Cmd'].reverse()
    else:next(m for m in new['Mounts'] if m['Destination']==str(op.GRANT))['RW']=True
    with pytest.raises(ValueError,match='RUNTIME_CONFIGURATION_CHANGED') as error:op.verify_runtime(old,new,{})
    assert field in error.value.differing_fields and not capsys.readouterr().out

def test_duplicate_mount_targets_cannot_be_hidden_by_order_normalization(candidate_values):
    old,new=runtimes();new['Mounts'].append(deepcopy(new['Mounts'][0]))
    with pytest.raises(ValueError,match='DUPLICATE_MOUNT_DESTINATION'):op.verify_runtime(old,new,{})

def test_private_full_inspect_is_saved_at_0600_and_only_reference_is_reported(capsys):
    # Root filesystem ownership/mode is the explicit seam. The actual frozen
    # writer enforces safe paths, root ownership, fsync and atomic replacement.
    class RootFile:
        def __init__(self,name):self.name=name
        def __str__(self):return '/owned/root-evidence/'+self.name
        def stat(self):return SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o600)
    class Folder:
        def __truediv__(self,name):return RootFile(name)
    saved={}
    def write(file,raw,mode):saved.update(file=str(file),raw=raw,mode=mode)
    writer=SimpleNamespace(save=write,read=lambda file:saved['raw'])
    info={'Id':'owned-only','Config':{'Env':['OWNED_SECRET=fixture-only']},'HostConfig':{'PidsLimit':256}}
    result=op.save_runtime_snapshot(writer,Folder(),info,'runtime-before.full.json')
    assert json.loads(saved['raw'])==info and saved['mode']==0o600
    assert result['mode']=='0600' and result['sha256']==op.sha(saved['raw'])
    assert 'OWNED_SECRET' not in json.dumps(result) and not capsys.readouterr().out

def test_release_allows_exact_three_sources_and_refuses_policy_or_runtime_scope_change():
    stable='services/hub/replay_parts_worker_runtime_v5.py'
    before={'test_attestation':{'sha256':op.OLD_PROOF_SHA},'authorization_sha256':op.OWNER_SHA,
        'source_files':{stable:'f'*64,**{n:'0'*64 for n in op.SOURCES}}}
    after={'source_files':{stable:'f'*64,**op.SOURCES}};op.validate_release_change(before,after)
    after['source_files'][stable]='e'*64
    with pytest.raises(ValueError,match='ONLY_THREE'):op.validate_release_change(before,after)

def test_exact_parent_plus_one_layer_and_unfilled_qualification_constants_stay_closed(monkeypatch):
    config={'User':'10001:10001','Env':['OWNED=fixture'],'Cmd':['owned'],'Labels':{'owned':'fixture'}}
    original={'Id':op.OLD_IMAGE,'RootFS':{'Layers':['flat-copy','workdir','parent-delta']},'Config':deepcopy(config),'Os':'linux','Architecture':'amd64'}
    candidate={'Id':op.IMAGE,'RootFS':{'Layers':['flat-copy','workdir','parent-delta','current-delta']},'Config':deepcopy(config),'Os':'linux','Architecture':'amd64'}
    op.validate_image_lineage(original,candidate)
    for change in ('ancestry','config','platform'):
        bad=deepcopy(candidate)
        if change=='ancestry':bad['RootFS']['Layers'][0]='foreign-parent'
        elif change=='config':bad['Config']['Env'].append('EXTRA=refused')
        else:bad['Architecture']='arm64'
        with pytest.raises(ValueError):op.validate_image_lineage(original,bad)
    monkeypatch.setattr(op,'IMAGE','PENDING_ACTUAL_QUALIFIED_IMAGE')
    assert not op.activation_constants_ready()
    with pytest.raises(ValueError,match='STILL_PENDING'):op.validate_qualifications(None,b'',b'',b'',b'',{})
