"""Pure host-delta validation: no Docker, SSH, DB writes or native actions."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


def module():
    path=Path(__file__).resolve().parents[2]/'scripts/ops/apply_saved_draft_navigation_wait_v3.py'
    spec=importlib.util.spec_from_file_location('owned_navigation_apply',path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


def compose(m):
    runtime={'environment':{'HUB_CONFIG':'/owned/config.toml'},'tmpfs':['/tmp:rw,size=1m'],
        'entrypoint':['owned-entry'],'volumes':[{'type':'bind','source':'/owned/original.mjs',
        'target':str(m.ROOT/m.NAV),'read_only':True,'bind':{'create_host_path':False}}]}
    return {'services':{name:deepcopy(runtime) for name in ('video-worker',*m.OTHERS)}}


def runtime(m):
    return {'Id':'old','Image':m.IMAGE,'State':{'Running':True},'Config':{'User':'10001:10001',
        'Cmd':['owned'],'Entrypoint':['owned-entry'],'WorkingDir':'/owned','Env':['OWNED=value','HOME=/tmp/highest/home'],
        'Hostname':'old-derived'},'HostConfig':{key:None for key in m.HOST_FIELDS},
        'Mounts':[{'Type':'bind','Source':'/owned/old.mjs','Destination':str(m.ROOT/m.NAV),'RW':False},
            {'Type':'tmpfs','Source':'','Destination':'/tmp/highest/home','RW':True}]}


def test_candidate_changes_one_existing_ro_source_and_restores_exact_original():
    m=module();before=compose(m);after=m.candidate(before,Path('/owned/new.mjs'))
    assert before['services']['video-worker']['volumes'][0]['source']=='/owned/original.mjs'
    reverse=deepcopy(after);reverse['services']['video-worker']['volumes'][0]['source']='/owned/original.mjs'
    assert reverse==before and all(after['services'][n]==before['services'][n] for n in m.OTHERS)


@pytest.mark.parametrize('case',['missing','duplicate','writable'])
def test_missing_duplicate_or_rw_existing_source_rejected(case):
    m=module();before=compose(m);values=before['services']['video-worker']['volumes']
    if case=='missing':values.clear()
    elif case=='duplicate':values.append(deepcopy(values[0]))
    else:values[0]['read_only']=False
    with pytest.raises(AssertionError):m.candidate(before,Path('/owned/new.mjs'))


def test_runtime_keeps_env_values_resources_mounts_and_ignores_only_derived_hostname():
    m=module();before=runtime(m);after=deepcopy(before);after['Id']='new';after['Config']['Hostname']='new-derived'
    after['Config']['Env'].reverse();after['Mounts'][0]['Source']=str(Path('/owned/new.mjs'))
    m.validate_runtime(before,after,Path('/owned/new.mjs'))


@pytest.mark.parametrize('case',['env','duplicate_env','tmpfs','entry','other_mount','wrong_image'])
def test_unrelated_runtime_or_environment_changes_are_refused(case):
    m=module();before=runtime(m);after=deepcopy(before);after['Mounts'][0]['Source']=str(Path('/owned/new.mjs'))
    if case=='env':after['Config']['Env'][0]='OWNED=changed'
    elif case=='duplicate_env':after['Config']['Env'].append(after['Config']['Env'][0])
    elif case=='tmpfs':after['HostConfig']['Tmpfs']={'/tmp/highest/home':'rw,size=unbounded'}
    elif case=='entry':after['Config']['Entrypoint']=['changed']
    elif case=='other_mount':after['Mounts'][1]['RW']=False
    else:after['Image']='unqualified-image'
    with pytest.raises(AssertionError):m.validate_runtime(before,after,Path('/owned/new.mjs'))
