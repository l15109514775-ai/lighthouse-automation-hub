import json
import os
from pathlib import Path
import subprocess


def test_independent_static_original_media_adapter(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/machine_media_fixture.mjs'),str(tmp_path/'machine-media')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},
        capture_output=True,timeout=70)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==32
    assert record['external_calls']==record['publish_requests']==record['model_calls']==0
    assert record['human_review'] is False and record['image_text_translation_performed'] is False
