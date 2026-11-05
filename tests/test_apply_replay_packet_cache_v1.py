from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.ops import apply_replay_packet_cache_v1 as c
from scripts.ops import deploy_highest_video_worker_v2 as d


def config():
    return {'name':'Owned','services':{**{key:{'image':'Owned unchanged'} for key in d.CORE},
        'video-worker':d.new_video_service(Path('/owned/key'),{c.MACHINE:b'Owned original'})}}


def test_only_two_readonly_code_mounts_and_exact_bounded_environment_tmpfs_change():
    before=config();saved=deepcopy(before);target=d.INSTALL/'deltas'/('packet-cache-'+c.COMMIT)
    after=c.candidate(before,target,d)
    assert before==saved and all(after['services'][name]==before['services'][name] for name in d.CORE)
    assert after['services']['video-worker']['command']==before['services']['video-worker']['command']
    assert after['services']['video-worker']['environment']==before['services']['video-worker']['environment']
    volumes=after['services']['video-worker']['volumes']
    for name in c.PINS:
        matches=[v for v in volumes if v['target']==str(d.ROOT/name)]
        assert len(matches)==1 and matches[0]['source']==str(target/name) and matches[0]['read_only'] is True
    tmpfs=dict(x.split(':',1) for x in after['services']['video-worker']['tmpfs'])
    assert all(c.options(tmpfs[name])==c.options(value) for name,value in c.DIRECTORIES.items())
    assert len(tmpfs)==5


@pytest.mark.parametrize('change',['wrong_source','writable','duplicate_cache'])
def test_unknown_or_writable_prior_code_is_not_rewritten(change):
    before=config();volumes=before['services']['video-worker']['volumes'];machine=next(v for v in volumes if v['target']==str(d.ROOT/c.MACHINE))
    if change=='wrong_source':machine['source']='/Owned unknown.py'
    elif change=='writable':machine['read_only']=False
    else:volumes.append(d.bind('/owned/cache',d.ROOT/c.CACHE,True))
    with pytest.raises(AssertionError):c.candidate(before,d.INSTALL/'owned',d)


@pytest.mark.parametrize('change',['none','extra_target','wrong_uid','too_large'])
def test_legacy_security_validator_is_only_adapted_after_exact_three_tmpfs_validation(change):
    before=config();after=c.candidate(before,d.INSTALL/'owned',d);service=after['services']['video-worker']
    actual=dict(item.split(':',1) for item in service['tmpfs']);value={'HostConfig':{'Tmpfs':deepcopy(actual)}};saved=deepcopy(value);calls=[]
    if change=='extra_target':value['HostConfig']['Tmpfs']['/Owned extra']='rw'
    elif change=='wrong_uid':value['HostConfig']['Tmpfs']['/tmp/highest/home']=c.DIRECTORIES['/tmp/highest/home'].replace('uid=10001','uid=0')
    elif change=='too_large':value['HostConfig']['Tmpfs']['/tmp/highest/cache']=c.DIRECTORIES['/tmp/highest/cache'].replace('size=32m','size=1g')
    prior=SimpleNamespace(ROOT=d.ROOT,validate_running=lambda val,s,code:calls.append(deepcopy(val)))
    if change=='none':
        c.validate_running(prior,value,service)
        assert set(calls[0]['HostConfig']['Tmpfs'])=={'/tmp',str(d.ROOT/'data/browser-profiles')} and value==saved
    else:
        with pytest.raises(AssertionError):c.validate_running(prior,value,service)
        assert calls==[]


def test_runtime_environment_verification_has_no_creation_or_permission_side_effect():
    compile(c.VERIFY_ENV,'Owned environment verify','exec')
    assert 'mkdir' not in c.VERIFY_ENV and 'chmod' not in c.VERIFY_ENV and 'chown' not in c.VERIFY_ENV
    assert "stat.S_IMODE(path.stat().st_mode)==0o700" in c.VERIFY_ENV
    assert 'os.access(path,os.W_OK)' in c.VERIFY_ENV


@pytest.mark.parametrize('change',['none','different_core','missing_runtime'])
def test_exact_production_dependency_mix_must_match_actual_runtime_not_only_two_delta_hashes(change):
    expected={'Owned current':'old','Owned machine':'new','Owned cache':'cache'}
    sources={name:Path('/Owned')/name for name in expected}
    content={file:expected[name] for name,file in sources.items()}
    mixed={'manifest':{'candidate_runtime_source_sha256':expected}}
    if change=='different_core':content[sources['Owned current']]='other separately-tested-core'
    elif change=='missing_runtime':sources.pop('Owned current')
    host=SimpleNamespace(read=lambda file,limit:content[file],sha=lambda raw:raw)
    if change=='none':c.validate_mixed_sources(host,sources,mixed)
    else:
        with pytest.raises(AssertionError):c.validate_mixed_sources(host,sources,mixed)
