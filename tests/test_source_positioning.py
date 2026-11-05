"""Exercise actual source, media and complete reply adapters on owned pages."""
import json
import os
from pathlib import Path
import subprocess
import pytest


@pytest.mark.timeout(180)
def test_native_source_positioning_keeps_strict_visibility_semantic_and_reply_boundaries(tmp_path):
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        ['node', str(root / 'tests/source_positioning_fixture.mjs'), str(tmp_path / 'source-positioning')],
        cwd=root,
        env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root / 'compositor/runtime/browsers'),
             'TEMP': str(tmp_path), 'TMP': str(tmp_path), 'TMPDIR': str(tmp_path)},
        capture_output=True, timeout=150,
    )
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    result = json.loads(proc.stdout)
    assert len(result['passed']) == len(set(result['passed'])) == 26
    assert result['external_requests'] == result['model_calls'] == result['publish_requests'] == 0
    cases = {case['name']: case for case in result['summaries']}
    assert cases['unpositioned-original-strict-guard-still-rejects-sticky']['result'] == 'rejected'
    assert cases['independent-capture-centers-then-original-strict-passes']['result'] == 'captured'
    assert cases['public-media-native-image-scroll-requires-source-recoordination']['result'] == 'captured'
    assert cases['reply-joint-position-plus-real-independent-parent-reread']['parent_navigations'] == 1
    assert cases['reply-each-node-fits-but-joint-range-too-tall']['result'] == 'rejected'
    assert cases['complete-extended-reply-orchestration-keeps-joint-capture-and-parent-reread']['reply_navigations'] == 3
    assert cases['extended-final-reused-source-screenshot-cannot-hide-body-change']['result'] == 'rejected'


def test_declared_xvfb_screen_can_contain_the_fixed_public_browser_viewport():
    root = Path(__file__).resolve().parents[1]
    # A local declaration check only; this does not claim a Windows test
    # started Xvfb or proved Linux framebuffer behavior.
    source = (root / 'adapters/x_web.py').read_text(encoding='utf-8')
    assert '1366x1800x24' in source
