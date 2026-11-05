import json
import os
from pathlib import Path
import subprocess


def test_real_owned_browser_rebinds_article_identity_during_hydration_without_relaxing_visibility(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/browser_identity_binding_fixture.mjs'),str(tmp_path/'binding')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers'),
                     'TEMP':str(tmp_path),'TMP':str(tmp_path),'TMPDIR':str(tmp_path)},
        capture_output=True,timeout=40)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    value=json.loads(result.stdout)
    assert len(value['passed'])==9 and value['external_calls']==0
    assert value['human_review'] is False and value['source_approval'] is False
    assert value['model_calls']==0 and value['publish_requests']==0
