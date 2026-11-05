import json,os,subprocess
from pathlib import Path
import pytest

@pytest.mark.parametrize('scenario',['nonempty','duplicate','wrong_source','viewer_not_owner'])
def test_later_lookup_reads_nonempty_manager_and_real_owned_video(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(root/'tests/replay_later_lookup_v2_fixture.mjs'),scenario,str(tmp_path)],cwd=root,env=env,
        capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1]);assert value['scenario']==scenario
    assert value['owned'] and value['real_video'] and value['external_requests']==value['native_clicks']==value['publications']==0
