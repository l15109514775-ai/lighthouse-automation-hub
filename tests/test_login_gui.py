import importlib.util
import json
from pathlib import Path
import pytest


def module():
    path=Path(__file__).resolve().parents[1]/'scripts/login-gui/session.py'
    spec=importlib.util.spec_from_file_location('owned_gui_session',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def prepared(runtime,monkeypatch,prior=0):
    settings,store=runtime;gui=module();gui.ROOT=settings.root;gui.STATE=settings.root/'data/x-login-session/state.json'
    gui.STATE.parent.mkdir();password=gui.STATE.parent/'password.txt';password.write_text('owned temporary fixture')
    with store.transaction() as db:
        db.execute("UPDATE pipelines SET paused=1 WHERE name='tweet'")
        seq=db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(0,'temporary-login','pause_for_x_login','tweet','owned fixture')).lastrowid
    gui.STATE.write_text(json.dumps({'state':'active','prior_paused':prior,'pause_audit_seq':seq}))
    commands=[];monkeypatch.setattr(gui.subprocess,'run',lambda args,**kwargs:commands.append(args))
    return gui,store,commands,password


def test_gui_cleanup_closes_only_own_container_then_restores_tweet(runtime,monkeypatch):
    gui,store,commands,password=prepared(runtime,monkeypatch)
    gui.cleanup()
    assert commands==[['docker','stop','-t','25','lighthouse-x-login-session'],['docker','rm','-f','lighthouse-x-login-session']]
    assert store.query("SELECT paused FROM pipelines WHERE name='tweet'")[0]['paused']==0
    assert not password.exists() and json.loads(gui.STATE.read_text())['profile_preserved'] is True
    assert all(r['paused']==0 for r in store.query("SELECT * FROM pipelines WHERE name<>'tweet'"))
    gui.cleanup();assert len(commands)==2


def test_gui_cleanup_preserves_previous_pause(runtime,monkeypatch):
    gui,store,_,_=prepared(runtime,monkeypatch,prior=1);gui.cleanup()
    assert store.query("SELECT paused FROM pipelines WHERE name='tweet'")[0]['paused']==1


def test_gui_cleanup_respects_newer_maintenance_pause(runtime,monkeypatch):
    gui,store,_,_=prepared(runtime,monkeypatch)
    with store.transaction() as db:db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(1,'maintenance','pause_pipeline','tweet','ok'))
    gui.cleanup();assert store.query("SELECT paused FROM pipelines WHERE name='tweet'")[0]['paused']==1
    assert store.query("SELECT outcome FROM audit WHERE action='close_x_login'")[0]['outcome']=='kept_external_pause'


def test_gui_refuses_root_and_api_secret_environment(monkeypatch):
    gui=module();monkeypatch.setattr(gui.os,'getuid',lambda:0,raising=False)
    with pytest.raises(RuntimeError,match='NONROOT_GUI_REQUIRED'):gui.gui()
    monkeypatch.setattr(gui.os,'getuid',lambda:10001,raising=False);monkeypatch.setenv('MODEL_API_KEY','owned synthetic negative fixture')
    with pytest.raises(RuntimeError,match='GUI_ENVIRONMENT_CONTAINS_API_SECRET'):gui.gui()


def test_gui_orchestration_does_not_pause_without_sudo(monkeypatch):
    gui=module();monkeypatch.setattr(gui.os,'getuid',lambda:10001,raising=False)
    with pytest.raises(RuntimeError,match='HOST_ORCHESTRATOR_REQUIRES_SUDO'):gui.host()


def test_gui_bindings_and_browser_do_not_add_debug_or_secret_access():
    root=Path(__file__).resolve().parents[1];code=(root/'scripts/login-gui/session.py').read_text('utf-8')
    browser=(root/'scripts/login-gui/browser.mjs').read_text('utf-8');unit=(root/'scripts/login-gui/lighthouse-x-login-gui.service').read_text('utf-8')
    assert "'127.0.0.1:6081','127.0.0.1:5901'" in code and "'-listen','127.0.0.1'" in code
    assert 'env_file' not in code and 'secrets.env' not in code and 'docker.sock' not in code
    assert 'chromiumSandbox:true' in browser and '--remote-debugging-port' not in browser and 'fill(' not in browser
    assert 'ExecStopPost=' in unit and 'RuntimeMaxSec=1280' in unit and 'Restart=no' in unit
