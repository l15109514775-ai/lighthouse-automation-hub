"""Owned configuration/rollback boundaries; no Docker or production state."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import ast,tomllib
import pytest
from scripts.ops import deploy_natural_replay_v3 as d


def test_only_four_top_level_values_change_preserving_bom_crlf_and_nested_settings():
    raw=b'\xef\xbb\xbf# Owned\r\nyoutube_natural_download_enabled = false # preserve\r\nother = "unchanged"\r\n[sources]\r\nyoutube_natural_download_enabled = false\r\n'
    changed=d.candidate_hub(raw);before=tomllib.loads(raw.decode('utf-8-sig'));after=tomllib.loads(changed.decode('utf-8-sig'))
    expected=deepcopy(before);expected.update(d.FLAGS)
    assert after==expected and changed.startswith(b'\xef\xbb\xbf# Owned\r\n')
    assert b'# preserve\r\n' in changed and b'[sources]\r\nyoutube_natural_download_enabled = false\r\n' in changed
    assert d.candidate_hub(changed)==changed


def test_invalid_top_level_flag_is_not_silently_reinterpreted():
    with pytest.raises(AssertionError):d.candidate_hub(b'youtube_natural_download_enabled = "not-a-switch"\n')


@pytest.mark.parametrize('change',['none','changed_download','omitted_change'])
def test_release_composition_rejects_source_mismatch_or_hidden_delta(change):
    base={'archive_sha256':'85','manifest':{'files':{'a':{'sha256':'a'}}}}
    download={'manifest':{'source_commit':d.RELEASE,'files':{'a':{'sha256':'b'}}}}
    integration={'manifest':{'source_freeze_commit':d.RELEASE,'previous_85_archive_sha256':'85',
        'files':{'a':{'sha256':'b'},'new':{'sha256':'c'}},'changed_from_85':['a','new']}}
    if change=='changed_download':integration['manifest']['files']['a']['sha256']='different'
    elif change=='omitted_change':integration['manifest']['changed_from_85']=['a']
    if change=='none':assert d.merged_sources(base,download,integration)==integration['manifest']['files']
    else:
        with pytest.raises(AssertionError):d.merged_sources(base,download,integration)


@pytest.mark.parametrize('change',['none','processing','old_lookup','old_parent_unknown','pilot_not_completed'])
def test_cutover_requires_both_role_idle_and_actual_completed_pilot(change):
    status={'video_processing':0,'old_lookup_runnable':0,'old_unresolved_publish':0,'pilot_completed':1}
    if change=='processing':status['video_processing']=1
    elif change=='old_lookup':status['old_lookup_runnable']=1
    elif change=='old_parent_unknown':status['old_unresolved_publish']=1
    elif change=='pilot_not_completed':status['pilot_completed']=0
    assert d.idle(status)==(change=='none')


def test_preflight_initializes_only_a_copy_after_all_consumption_and_network_gates():
    compile(d.PRECHECK,'Owned preflight','exec');ast.parse(d.PRECHECK)
    factory=d.PRECHECK.index('worker,service,downloader=build_worker')
    assert d.PRECHECK.index('Store.claim=Store.recover=Store.enqueue=Store.finish=refuse')<factory
    assert d.PRECHECK.index('QueueView.claim=QueueView.recover=threading.Thread.start=refuse')<factory
    assert d.PRECHECK.index('socket.socket.connect=refuse')<factory
    assert "Store('/run/preflight-db/hub.sqlite3')" in d.PRECHECK
    assert "load_youtube_key('/run/secrets/youtube-api-key')" in d.PRECHECK
    assert 'worker.resource_waits.initialize();worker.readiness_waits.initialize()' in d.PRECHECK


def fake_host(tmp_path,calls):
    root=tmp_path/'owned';root.mkdir()
    return SimpleNamespace(ROOT=root,safe=lambda file:file,compose=lambda *args:calls.append(args),
        atomic=lambda file,body,**kwargs:file.write_bytes(body))


def test_job_admitted_during_shutdown_keeps_new_configuration_and_restarts_new_consumer(tmp_path,monkeypatch):
    calls=[];host=fake_host(tmp_path,calls);compose=host.ROOT/'compose.yaml';compose.write_bytes(b'new runtime')
    snapshots=iter([{'natural_jobs':0,'video_processing':0},{'natural_jobs':1,'video_processing':0}])
    monkeypatch.setattr(d,'database_status',lambda *args:next(snapshots));monkeypatch.setattr(d,'core_processes',lambda *args:{'Owned':'unchanged'})
    assert d.rollback_if_safe(host,'Owned pilot',{},b'old runtime',None,{'Owned':'unchanged'},locks_held=True) is False
    assert calls==[('stop','--timeout','45','video-worker'),('up','-d','--no-deps','--no-build','video-worker')]
    assert compose.read_bytes()==b'new runtime'


def test_active_new_job_forbids_stop_or_configuration_rollback(tmp_path,monkeypatch):
    calls=[];host=fake_host(tmp_path,calls)
    monkeypatch.setattr(d,'database_status',lambda *args:{'natural_jobs':1,'video_processing':1})
    assert d.rollback_if_safe(host,'Owned pilot',{},b'old',None,{},locks_held=True) is False
    assert calls==[]


def test_safe_no_admission_rollback_restores_old_bytes_before_starting_old_consumer(tmp_path,monkeypatch):
    calls=[];host=fake_host(tmp_path,calls);compose=host.ROOT/'compose.yaml';compose.write_bytes(b'new runtime')
    policy=host.ROOT/'policy.toml';policy.write_bytes(b'new policy')
    monkeypatch.setattr(d,'database_status',lambda *args:{'natural_jobs':0,'video_processing':0})
    monkeypatch.setattr(d,'core_processes',lambda *args:{'Owned':'same'})
    def operation(*args):
        if args[0]=='up':assert compose.read_bytes()==b'old runtime' and policy.read_bytes()==b'old policy'
        calls.append(args)
    host.compose=operation
    assert d.rollback_if_safe(host,'Owned pilot',{policy:(b'old policy',None)},b'old runtime',None,{'Owned':'same'},locks_held=True)
    assert calls[-1]==('up','-d','--no-deps','--no-build','video-worker')


def test_bad_install_ancestor_is_rejected_before_any_filesystem_write(tmp_path,monkeypatch):
    calls=[];file=tmp_path/'Owned bad ancestor'/'file.py'
    def reject(path):calls.append('safe reject');raise AssertionError('Owned symlink rejection')
    monkeypatch.setattr(Path,'mkdir',lambda *args,**kwargs:calls.append('mkdir'))
    monkeypatch.setattr(d.os,'chown',lambda *args,**kwargs:calls.append('chown'),raising=False)
    monkeypatch.setattr(Path,'chmod',lambda *args,**kwargs:calls.append('chmod'))
    with pytest.raises(AssertionError):d.install_file(SimpleNamespace(safe=reject),file,b'Owned')
    assert calls==['safe reject']


def test_symlink_role_open_failure_cannot_chown_or_chmod_target(tmp_path,monkeypatch):
    calls=[];sequence=iter([10,20,30])
    monkeypatch.setattr(d,'protected',lambda path:None)
    def open_dir(path,*args,**kwargs):
        if path=='moderation':raise OSError('Owned O_NOFOLLOW rejection')
        return next(sequence)
    def exists(*args,**kwargs):raise FileExistsError()
    monkeypatch.setattr(d.os,'open',open_dir);monkeypatch.setattr(d.os,'mkdir',exists)
    monkeypatch.setattr(d.os,'fchown',lambda *args:calls.append('fchown'),raising=False)
    monkeypatch.setattr(d.os,'fchmod',lambda *args:calls.append('fchmod'),raising=False)
    monkeypatch.setattr(d.os,'close',lambda fd:calls.append(('close',fd)))
    with pytest.raises(OSError):d.role_lock(SimpleNamespace(ROOT=tmp_path),'moderation')
    assert calls==[('close',30),('close',20),('close',10)]
