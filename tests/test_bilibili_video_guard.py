"""Read-only native Chromium DOM guard fixture; no live platform account."""
import json
import os
from pathlib import Path
import subprocess


def test_owned_dom_video_protection_guard_known_wrapper_and_real_challenges(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root/'tests/bilibili_video_guard_fixture.mjs'), str(tmp_path/'guard')],
        cwd=root, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root/'compositor/runtime/browsers')},
        capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    data = json.loads(result.stdout)
    assert len(data['passed']) >= 25 and len(set(data['passed'])) == len(data['passed'])
    assert 'empty_official_manager_with_three_visible_canonical_links_is_document_only' in data['passed']
    assert 'empty_official_manager_still_blocks_child_verification' in data['passed']
    assert data['external_network_calls'] == data['real_uploads'] == data['clicks'] == 0
    assert data['storage_reads'] == data['cookie_reads'] == 0
    assert data['routed_requests'] > 0
    assert not data['live_tested'] and not data['human_review'] and not data['publication_authorized']
