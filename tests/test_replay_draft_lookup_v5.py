"""Real owned Chromium replay of the observed native draft DOM; zero sends."""
from pathlib import Path
import json,os,subprocess
import pytest

@pytest.mark.parametrize('scenario',['found','duplicate_title','wrong_source','wrong_uid','cover_mismatch','multiple_pages','not_found','empty','challenge','unicode_mismatch','public_inventory_incomplete'])
def test_native_draft_tab_and_exact_source_editor_are_read_only(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA','ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    p=subprocess.run(['node',str(root/'tests/replay_draft_lookup_v5_fixture.mjs'),scenario,str(tmp_path)],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert p.returncode==0,p.stderr
    result=json.loads(p.stdout.strip().splitlines()[-1])
    assert result['owned'] and result['external_requests']==result['public_submits']==result['choosers']==0
