import json
import os
from pathlib import Path
import subprocess


def test_expiry_preflight_only_skips_twice_visible_certainly_expired_sources(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/expiry_preflight_fixture.mjs'),str(tmp_path/'expiry-preflight')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),
                     'TEMP':str(tmp_path),'TMP':str(tmp_path),'TMPDIR':str(tmp_path)},
        capture_output=True,timeout=65)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    value=json.loads(result.stdout)
    assert len(value['passed'])==18 and value['external_requests']==0
