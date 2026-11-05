import json,os,subprocess
from pathlib import Path
import pytest

@pytest.mark.parametrize('scenario',['save_current','save_all','unknown_save','duplicate_task','guard','known_info','other_modal'])
def test_only_two_bound_cached_private_tasks_can_be_saved(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    r=subprocess.run(['node',str(root/'tests/cached_separate_private_v5_fixture.mjs'),scenario,str(tmp_path)],cwd=root,env=env,
        capture_output=True,text=True,encoding='utf-8',timeout=100)
    assert r.returncode==0,r.stderr
    value=json.loads(r.stdout.strip().splitlines()[-1]);assert value['scenario']==scenario and value['owned']
    assert value['public_clicks']==value['file_choosers']==value['external_requests']==0 and value['original_evidence_unchanged']
