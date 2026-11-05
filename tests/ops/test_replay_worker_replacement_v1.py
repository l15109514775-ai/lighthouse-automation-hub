"""Owned cutover-plan fixtures. Never Docker, root actions, upload or server DB."""
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path,PurePosixPath
from types import SimpleNamespace

import pytest

REPO=Path(__file__).resolve().parents[2];BASE=REPO/'scripts/ops'
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v);return v
m=load('replacement_owned',BASE/'replay_worker_replacement_v1.py')
pilot=load('pilot_fixture',REPO/'scripts/ops/deploy_youtube_acceptance_pilot_v1.py')
monitor=load('monitor_fixture',REPO/'scripts/ops/deploy_youtube_monitor_refresh_v1.py')
for v in (pilot,monitor):v.ROOT=PurePosixPath('/opt/lighthouse-hub')
pilot.INSTALL=PurePosixPath('/usr/local/lib/lighthouse-video-pilot-v1')
RAW=monitor.mutate_compose(pilot.mutate_compose((REPO/'compose.yaml').read_bytes()))

def before():
    common={'image':'lighthouse-hub:0.1.0','restart':'unless-stopped','environment':{'OWNED_ONLY':'not-a-real-secret'},
        'volumes':[{'type':'bind','source':'/owned/data','target':'/opt/lighthouse-hub/data'}]}
    return {'name':'lighthouse-hub','services':{n:deepcopy(common) for n in ('api','scheduler','tweet-worker','system-worker','video-worker')}}
def after(value):
    v=deepcopy(value);v['services']['video-worker'].update(profiles=[m.LEGACY],restart='no');return v

def test_only_legacy_worker_profile_and_restart_change():
    out=m.legacy_compose(RAW)
    assert out.count(b'profiles: ["legacy-video-worker-rollback"]')==1
    assert out.count(b'restart: "no"')==1
    for raw in (b'lighthouse-video-pilot-v1',b'lighthouse-youtube-monitor-v1',b'./data/browser-profiles/youtube'):
        assert out.count(raw)==RAW.count(raw)
    m.assert_legacy_delta(before(),after(before()))

@pytest.mark.parametrize('change',['api_env','scheduler_restart','tweet_image','video_mount'])
def test_unrelated_compose_changes_are_rejected(change):
    base=before();out=after(base)
    if change=='api_env':out['services']['api']['environment']['OWNED_ONLY']='changed'
    elif change=='scheduler_restart':out['services']['scheduler']['restart']='no'
    elif change=='tweet_image':out['services']['tweet-worker']['image']='unreviewed'
    else:out['services']['video-worker']['volumes']=[]
    with pytest.raises(AssertionError):m.assert_legacy_delta(base,out)

def test_second_cutover_refuses_existing_legacy_profile():
    with pytest.raises(AssertionError):m.legacy_compose(m.legacy_compose(RAW))

def test_child_container_has_no_control_endpoint_or_full_secrets(monkeypatch):
    monkeypatch.setattr(m,'Path',PurePosixPath)
    monkeypatch.setattr(m,'ROOT',PurePosixPath('/opt/lighthouse-hub'))
    monkeypatch.setattr(m,'INSTALL',PurePosixPath('/usr/local/lib/lighthouse-replay-video-worker-v1'))
    args=m.docker_create(PurePosixPath('/etc/lighthouse-hub/youtube-only.key'),{**m.EXPECTED_RUNTIME,m.ENTRY:m.ENTRY_SHA})
    joined='\n'.join(args)
    assert args[-4:]==[m.IMAGE,'python','-m','services.hub.replay_worker_runtime']
    assert '--read-only' in args and '--restart=no' in args and '--cap-drop' in args
    assert '--env-file' not in args and '--publish' not in args and '-p' not in args and '--privileged' not in args
    assert 'docker.sock' not in joined and 'secrets.env' not in joined and 'YOUTUBE_API_KEY=' not in joined
    assert 'target=/run/secrets/youtube-api-key,readonly' in joined
    assert 'data/browser-profiles/youtube,readonly' in joined
    assert 'data/browser-profiles:rw,size=1m,mode=0700,uid=10001,gid=10001' in joined
    assert not any(n.startswith(('tests/','config/')) for n in m.EXPECTED_RUNTIME)

def test_systemd_requires_old_dispatcher_preflight_before_start():
    unit=m.unit_text().decode()
    assert '--verify-installed' in unit and 'docker start -a '+m.NAME in unit
    assert 'Restart=always' in unit and 'ProtectSystem=strict' in unit and 'ExecStop=/usr/bin/docker stop --time 45' in unit
    assert 'EnvironmentFile=' not in unit and 'YOUTUBE_API_KEY=' not in unit

@pytest.fixture
def completed(tmp_path,monkeypatch):
    root=tmp_path/'hub';file=root/'media'/m.DOWNLOAD/'youtube-download-v1/original.mp4';file.parent.mkdir(parents=True)
    file.write_bytes(b'owned source bytes, not a real stream')
    binding={'job_id':m.DOWNLOAD,'video_id':m.PILOT}
    asset={'file':str(file),'bytes':file.stat().st_size,'sha256':m.sha(file.read_bytes())}
    receipt={'source_to_merged_packet_coverage_verified':True}
    verified={'binding_sha256':m.sha(m.wire(binding)),'source_asset':asset,'receipt':receipt}
    (file.parent/'verified.json').write_bytes(m.wire(verified));monkeypatch.setattr(m,'ROOT',root)
    job={'job_id':m.DOWNLOAD,'source_id':m.PILOT,'source_platform':'youtube','job_type':'youtube_download','state':'completed',
        'result':{'youtube_download_v1':{'version':'youtube-replay-download-v1','state':'verified','binding':binding,'source_asset':asset,'receipt':receipt}}}
    return root,file,{'download':job}

def test_completed_source_is_actually_hashed_and_receipt_bound(completed):
    _,file,value=completed
    assert m.completed_download(value)['asset_sha256']==m.sha(file.read_bytes())
    file.write_bytes(b'owned modified bytes')
    with pytest.raises(AssertionError):m.completed_download(value)

@pytest.mark.parametrize('state',['processing','pending','retry_scheduled','awaiting_review','publish_unknown','failed'])
def test_stopped_legacy_does_not_mean_incomplete_long_download_finished(completed,state):
    _,_,value=completed;value['download']['state']=state
    with pytest.raises(AssertionError):m.completed_download(value)

def test_mismatched_verified_receipt_blocks_cutover(completed):
    _,file,value=completed
    p=file.parent/'verified.json';v=json.loads(p.read_bytes());v['binding_sha256']='0'*64;p.write_bytes(m.wire(v))
    with pytest.raises(AssertionError):m.completed_download(value)

def test_rollback_refuses_any_new_kind_runnable_before_stop(tmp_path,monkeypatch):
    root=tmp_path/'hub';install=tmp_path/'install';install.mkdir();backup=root/'backups/replay-worker-replacement/11111111-1111-1111-1111-111111111111';backup.mkdir(parents=True)
    (backup/'compose.yaml.before').write_bytes(RAW)
    manifest={'state':'replacement_installed','image':m.IMAGE,'rollback_directory':str(backup),'compose_before_sha256':m.sha(RAW)}
    (install/'manifest.json').write_bytes(m.wire(manifest))
    monkeypatch.setattr(m,'ROOT',root);monkeypatch.setattr(m,'INSTALL',install)
    monkeypatch.setattr(m,'protected',lambda p:None);monkeypatch.setattr(m,'locks',lambda:nullcontext())
    monkeypatch.setattr(m,'dbquery',lambda:{'active':[],'new_replay_unresolved':[{'state':'pending'}]})
    calls=[];monkeypatch.setattr(m,'run',lambda *a,**k:calls.append(a))
    with pytest.raises(AssertionError):m.rollback()
    assert not calls
