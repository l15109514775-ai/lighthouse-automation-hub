import json
import os
from pathlib import Path
import subprocess


def test_exact_visible_link_normalization_with_owned_browser_html(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/machine_link_fixture.mjs')],cwd=root,
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},capture_output=True,timeout=40)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==22
    assert record['external_calls']==record['model_calls']==record['publish_requests']==0
    assert record['human_review'] is False and record['url_targets_dereferenced'] is False
