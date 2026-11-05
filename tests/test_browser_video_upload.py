"""Owned .test page fixture, no platform connection or account credentials."""
import json
import os
from pathlib import Path
import subprocess


def test_owned_video_page_driver_native_controls_and_fail_closed_boundaries(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/browser_video_upload_fixture.mjs'),str(tmp_path/'video-page')],
        cwd=root,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')},
        capture_output=True,timeout=70)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    data=json.loads(result.stdout)
    assert len(data['passed'])==30
    assert data['external_calls']==0 and data['real_uploads']==0
    assert not data['live_tested'] and not data['human_review'] and not data['publication_authorized']
