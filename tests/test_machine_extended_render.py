import json
import os
from pathlib import Path
import subprocess


def test_prototype_extended_renderer_binds_reply_media_and_all_evidence(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/machine_extended_render_fixture.mjs'),str(tmp_path/'machine-extended')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},
        capture_output=True,timeout=240)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==20
    assert record['external_calls']==record['model_calls']==record['publish_requests']==0
    assert record['human_review'] is False and record['publication_authorized'] is False
