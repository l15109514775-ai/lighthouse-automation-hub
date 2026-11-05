import json
import os
from pathlib import Path
import subprocess


def test_public_dom_full_dimensions_without_original_upload_claim(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/machine_media_public_fixture.mjs'),str(tmp_path/'public-media')],cwd=root,
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},capture_output=True,timeout=70)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==19
    assert record['external_calls']==record['model_calls']==record['publish_requests']==0
    assert record['human_review'] is False and record['creator_original_upload_identity_verified'] is False
