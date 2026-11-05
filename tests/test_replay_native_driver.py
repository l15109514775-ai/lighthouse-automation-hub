"""Real Chromium on entirely routed synthetic pages. Never a live account."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario',['upload_once','wrong_uid','challenge','txt_chooser','prepare_and_submit','submit_unknown','missing_reservation','source_changed','cover_sync','public_lookup','public_challenge'])
def test_replay_native_stages_and_one_use_boundaries(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA','ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(root/'tests/replay_native_driver_fixture.mjs'),str(tmp_path/'owned'),scenario],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=90)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario']==scenario
    assert value['external_network_calls']==value['unowned_http_responses']==0
    assert value['live_tested'] is value['real_owner_approval'] is value['human_review'] is False
