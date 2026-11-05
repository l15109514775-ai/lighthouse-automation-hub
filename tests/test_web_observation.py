import json
import os
from pathlib import Path
import subprocess


def test_real_browser_overlay_and_source_isolation_with_owned_html(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/web_observation_fixture.mjs'),str(tmp_path/'evidence')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},
        capture_output=True,timeout=35)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    record=json.loads(result.stdout)
    assert len(record['passed'])==15 and record['external_calls']==0 and record['human_review'] is False
    assert (tmp_path/'evidence/dismissible/prompt-0-before.png').is_file()
    assert (tmp_path/'evidence/dismissible/source-visible.png').is_file()
    assert (tmp_path/'evidence/unknown/source-obscured.png').is_file()
