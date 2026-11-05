import json
import os
from pathlib import Path
import subprocess


def test_fresh_public_x_sessions_do_not_load_or_mutate_stored_profiles(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/public_x_session_fixture.mjs'),str(tmp_path/'owned-public-session')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),
                     'TEMP':str(tmp_path),'TMP':str(tmp_path),'TMPDIR':str(tmp_path)},
        capture_output=True,timeout=40)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    value=json.loads(result.stdout)
    assert len(value['passed'])==8 and value['external_requests']==0
    assert value['authentication_mode']=='public_anonymous_ephemeral'
    assert value['persistent_profile_used'] is False and value['human_review'] is False
