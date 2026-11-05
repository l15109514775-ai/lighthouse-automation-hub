"""Pure owned deployment-boundary checks. No Docker, account or production data."""
import builtins
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def deployer():
    source=Path(__file__).resolve().parents[2]/'scripts/ops/deploy_x_relations_overlay_v1.py'
    spec=importlib.util.spec_from_file_location('owned_x_relations_deployer',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def compose(module):
    common={'image':module.CBC,'environment':{'OWNED_SYNTHETIC':'not-a-real-secret'},'command':['python','worker'],
        'volumes':[{'type':'bind','source':'/owned/data','target':str(module.ROOT/'data'),'read_only':False},
            {'type':'bind','source':'/owned/old-x.py','target':str(module.ROOT/'adapters/x.py'),'read_only':True}]}
    return {'name':'owned-test','services':{n:deepcopy(common) for n in ('tweet-worker','api','scheduler','video-worker','system-worker')}}


def test_only_fourteen_readonly_source_bindings_change(deployer):
    before=compose(deployer);original=deepcopy(before);folder=Path('/usr/local/lib/owned-x/test')
    after=deployer.candidate(before,folder)
    assert before==original
    assert all(after['services'][n]==before['services'][n] for n in ('api','scheduler','video-worker','system-worker'))
    volumes=after['services']['tweet-worker']['volumes'];assert len(volumes)==15
    for name in deployer.PROD:
        selected=[v for v in volumes if v.get('target')==str(deployer.ROOT/name)]
        assert selected==[{'type':'bind','source':str(folder/name),'target':str(deployer.ROOT/name),
            'read_only':True,'bind':{'create_host_path':False}}]
    assert volumes[0]==before['services']['tweet-worker']['volumes'][0]


@pytest.mark.parametrize('fault',['duplicate','writable','volume'])
def test_bad_existing_source_mount_refuses_before_mutating_input(deployer,fault):
    value=compose(deployer);volumes=value['services']['tweet-worker']['volumes']
    if fault=='duplicate':volumes.append(deepcopy(volumes[-1]))
    elif fault=='writable':volumes[-1]['read_only']=False
    else:volumes[-1]['type']='volume'
    before=deepcopy(value)
    with pytest.raises(AssertionError):deployer.candidate(value,Path('/owned/candidate'))
    assert value==before


def runtime(module,folder):
    old={'Id':'a'*64,'Image':module.CBC,'State':{'Running':True},'Config':{'User':'10001:10001','Env':['OWNED_SYNTHETIC=value','OWNED_SECOND=value'],
        'Cmd':['python','worker'],'Entrypoint':None,'WorkingDir':'/owned','Hostname':'a'*12},
        'HostConfig':{'Memory':1400*1024**2,'ReadonlyRootfs':True,'Binds':['old'],
            'Mounts':[],'Tmpfs':{'/tmp':'rw,size=192m,mode=1777'}},
        'Mounts':[{'Destination':str(module.ROOT/'data'),'Source':'/owned/data','Type':'bind','RW':True}]}
    new=deepcopy(old);new['HostConfig']['Binds']=['new-source-bindings']
    new['Id']='b'*64;new['Config']['Hostname']='b'*12
    new['Mounts'] += [{'Destination':str(module.ROOT/n),'Source':str(folder/n),'Type':'bind','RW':False} for n in module.PROD]
    return old,new


def test_runtime_accepts_exact_bind_overlay_and_preserves_existing_resources(deployer):
    folder=Path('/owned/overlay');old,new=runtime(deployer,folder)
    deployer.runtime_unchanged(old,new,folder)


def test_explicit_hostname_stays_exact_and_environment_order_is_not_a_change(deployer):
    folder=Path('/owned/overlay');old,new=runtime(deployer,folder)
    old['Config']['Hostname']=new['Config']['Hostname']='owned-explicit'
    new['Config']['Env'].reverse()
    deployer.runtime_unchanged(old,new,folder,hostname='owned-explicit')
    new['Config']['Hostname']='b'*12
    with pytest.raises(AssertionError):deployer.runtime_unchanged(old,new,folder,hostname='owned-explicit')


def test_default_hostname_cannot_belong_to_an_unrelated_container(deployer):
    folder=Path('/owned/overlay');old,new=runtime(deployer,folder);new['Config']['Hostname']='a'*12
    with pytest.raises(AssertionError):deployer.runtime_unchanged(old,new,folder)


@pytest.mark.parametrize('fault',['memory','env','core_mount','source_writable','duplicate','image'])
def test_runtime_rejects_unrelated_or_writable_changes(deployer,fault):
    folder=Path('/owned/overlay');old,new=runtime(deployer,folder)
    if fault=='memory':new['HostConfig']['Memory']+=1
    elif fault=='env':new['Config']['Env'].append('OWNED_SYNTHETIC_EXTRA=value')
    elif fault=='core_mount':new['Mounts'][0]['RW']=False
    elif fault=='source_writable':new['Mounts'][-1]['RW']=True
    elif fault=='duplicate':new['Mounts'].append(deepcopy(new['Mounts'][-1]))
    else:new['Image']='sha256:'+'0'*64
    with pytest.raises(AssertionError):deployer.runtime_unchanged(old,new,folder)


def test_helper_executes_captured_hashed_bytes_without_import_reread(deployer,tmp_path,monkeypatch):
    file=tmp_path/'frozen-helper.py';raw=b"MARKER='owned-safe'\n";file.write_bytes(raw)
    monkeypatch.setattr(deployer,'HELPER_SHA',deployer.sha(raw))
    def compile_captured(body,name,mode):
        file.write_bytes(b"raise RuntimeError('owned-path-replaced')\n")
        assert body==raw;return builtins.compile(body,name,mode)
    monkeypatch.setattr(deployer,'compile',compile_captured,raising=False)
    module=deployer.load_helper(file)
    assert module.MARKER=='owned-safe'


def test_qualification_rejects_unrelated_evidence_path_before_read(deployer):
    class NoRead:
        safe=staticmethod(lambda p:p)
        @staticmethod
        def read(*args):pytest.fail('Wrong evidence path must fail before reading any file')
    with pytest.raises(AssertionError):deployer.qualification(NoRead(),'/owned/other/test-attestation.json','0'*64,'/owned/archive')


@pytest.mark.parametrize('fault',[None,'init','caps','no-new-privileges','profile'])
def test_qualification_keeps_actual_owned_container_security(deployer,fault):
    class Profile:
        @staticmethod
        def read(path):
            assert path==deployer.ROOT/'config/browser-seccomp.json';return b'{"defaultAction":"OWNED_ONLY"}'
    host={'Init':True,'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true','seccomp={"defaultAction":"OWNED_ONLY"}']}
    if fault=='init':host['Init']=False
    elif fault=='caps':host['CapDrop']=[]
    elif fault=='no-new-privileges':host['SecurityOpt'][0]='no-new-privileges:false'
    elif fault=='profile':host['SecurityOpt'][1]='seccomp={"defaultAction":"OWNED_WRONG"}'
    if fault:
        with pytest.raises(AssertionError):deployer.qualification_security(Profile(),host)
    else:deployer.qualification_security(Profile(),host)
