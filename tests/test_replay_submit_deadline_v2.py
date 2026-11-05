"""Submission-result wait budgets and actual Chromium close-before-click."""
import json
import math
import os
from pathlib import Path
import subprocess
import time

import pytest

from services.hub.errors import HubError
from services.hub.replay_video_browser import ReplayBrowserSession, submit_rpc_budget


def test_result_wait_uses_remaining_ticket_plus_window_without_extending_ticket():
    assert submit_rpc_budget({'expires_at':1300},now=1000)==360
    assert submit_rpc_budget({'expires_at':1900},now=1000)==600
    assert submit_rpc_budget({'expires_at':1001},now=1000)==61


@pytest.mark.parametrize('expires',[None,True,float('nan'),1000,1901])
def test_invalid_or_expired_ticket_never_gets_a_new_transport_window(expires):
    with pytest.raises(HubError):submit_rpc_budget({'expires_at':expires},now=1000)


def test_actual_operation_waits_past_old_120_and_keeps_original_request_and_ticket():
    session=object.__new__(ReplayBrowserSession);session.closed=False;sent=[];waits=[]
    session._send=lambda value:sent.append(value)
    def receive(timeout):
        waits.append(timeout)
        return {'request_id':sent[-1]['request_id'],'operation':'submit','ok':True,'result':{'state':'unknown'}}
    session._receive=receive
    ticket={'expires_at':time.time()+300}
    assert session._operation('submit',reservation=ticket)=={'state':'unknown'}
    assert 350<waits[0]<=360 and sent[0]['arguments']=={'reservation':ticket}
    assert sent[0]['operation']=='submit' and ticket.keys()=={'expires_at'}
    sent.clear()
    with pytest.raises(HubError):session._operation('submit',reservation={'expires_at':time.time()-1})
    assert sent==[]


@pytest.mark.parametrize('scenario',['delayed_success','expires_before_click','already_expired','eof_during_launch'])
def test_node_deadline_and_parent_disconnect_close_only_owned_context(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={key:os.environ[key] for key in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE',
        'ProgramData','PROGRAMDATA','ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE',
        'TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if key in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(root/'tests/replay_submit_deadline_v2_fixture.mjs'),scenario],
        cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario']==scenario and value['real_publications']==value['external_requests']==0
    assert value['live_tested'] is False
