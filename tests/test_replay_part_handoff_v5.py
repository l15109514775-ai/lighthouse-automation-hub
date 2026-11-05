"""Owned protected page handoff; no account, remote port or live GUI service."""
import json,os,subprocess
from pathlib import Path
import pytest

@pytest.mark.parametrize('scenario',['config','ticket','close_hung','verified','no_ack','expired','settles','body_unreadable'])
def test_v5_original_page_handoff_preserves_guard_and_never_repeats_action(scenario,tmp_path):
 root=Path(__file__).resolve().parents[1]
 env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
  'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','LANG','LC_ALL') if k in os.environ}
 env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
 p=subprocess.run(['node',str(root/'tests/replay_part_handoff_v5_fixture.mjs'),scenario,str(tmp_path)],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
 assert p.returncode==0,p.stderr
 result=json.loads(p.stdout.strip().splitlines()[-1]);assert result['scenario']==scenario and result['owned'] is True and result['platform_actions']==0
