import json
import os
from pathlib import Path
import subprocess


def test_owned_browser_machine_source_facts(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/machine_source_fixture.mjs'),str(tmp_path/'machine-source')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},
        capture_output=True,timeout=50)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==34 and record['external_calls']==0 and record['human_review'] is False


def test_real_machine_renderer_keeps_original_manual_checks(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/machine_render_fixture.mjs'),str(tmp_path/'machine-render')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},
        capture_output=True,timeout=180)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==9 and record['external_calls']==0 and record['publish_requests']==0
    assert record['human_review'] is False
