"""Native plaintext paste in a routed owned editor; no Bilibili request or send."""
import json
import os
from pathlib import Path
import subprocess


def test_scoped_native_caption_paste_preserves_linebreaks_and_treats_html_as_text():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/browser_caption_input_fixture.mjs')],
        cwd=root, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root / 'compositor/runtime/browsers')},
        capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    value = json.loads(result.stdout)
    assert len(value['passed']) == len(set(value['passed'])) == 12
    assert value['external_calls'] == value['model_calls'] == value['publish_requests'] == 0
    assert value['dom_html_written'] is False and value['native_method'] == 'scoped_clipboard_native_paste'
    assert value['scoped_permission'] is True and value['clipboard_origin'] == 'https://t.bilibili.com'
    assert value['real_site_loaded'] is False
    assert value['guard_negative_cases'] == ['focus-not-active', 'selection-absent', 'selected-range-outside']
