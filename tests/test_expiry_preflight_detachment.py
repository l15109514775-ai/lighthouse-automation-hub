import json
import os
from pathlib import Path
import subprocess


def test_public_dom_rerender_does_not_require_locator_screenshot_or_weaken_visibility(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/expiry_preflight_detachment_fixture.mjs'),str(tmp_path/'expiry-rerender')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),
                     'TEMP':str(tmp_path),'TMP':str(tmp_path),'TMPDIR':str(tmp_path)},
        capture_output=True,timeout=65)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    value=json.loads(result.stdout)
    assert len(value['passed'])==6 and value['external_requests']==0
