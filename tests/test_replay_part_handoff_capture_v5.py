"""Owned Chromium screenshot recovery, with the original masks and hard budget."""
import json,os,subprocess
from pathlib import Path
import pytest

@pytest.mark.parametrize('scenario',['retry','persistent_failure','other_error'])
def test_owner_capture_requires_real_masked_image_or_fails_within_original_budget(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','LANG','LC_ALL','XDG_CONFIG_HOME','XDG_CACHE_HOME') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    p=subprocess.run(['node',str(root/'tests/replay_part_handoff_capture_v5_fixture.mjs'),scenario,str(tmp_path)],
                     cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert p.returncode==0,p.stderr
    value=json.loads(p.stdout.strip().splitlines()[-1])
    assert value['owned'] and value['mask_preserved'] and value['bounded'] and value['platform_actions']==0
    assert value['real_png'] is (scenario=='retry')
