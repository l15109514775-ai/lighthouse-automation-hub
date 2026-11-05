"""Real owned Chromium loading transitions, no live site/account/actions."""
import json,os,subprocess
from pathlib import Path
import pytest

@pytest.mark.parametrize('scenario',[
    'empty_then_sidebar_then_manager','sidebar_then_manager','sidebar_timeout',
    'challenge_during_loading','authentication_during_loading','ordinary_dialog_during_loading',
])
def test_body_only_loading_waits_for_guard_readiness_and_never_ignores_protection(scenario,tmp_path):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(root/'tests/replay_part_loading_readiness_v5_fixture.mjs'),scenario,str(tmp_path)],
        cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario']==scenario and value['owned'] is True
    assert value['actions']==value['network_requests_sent']==0


@pytest.mark.parametrize('scenario',['manager_loading_then_public','manager_loading_timeout'])
def test_complete_later_lookup_keeps_its_twenty_second_passive_manager_budget(scenario,tmp_path):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(root/'tests/replay_later_manager_pending_v5_fixture.mjs'),scenario,str(tmp_path)],
        cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario']==scenario and value['owned'] is True
    assert value['external_requests']==value['clicks']==value['choosers']==0
