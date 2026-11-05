"""Pure production validator with owned revision records; no live platform."""
import json
from pathlib import Path
import subprocess

def test_versioned_metadata_chain_accepts_only_exact_public_line_removal():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root/'tests/short_video_metadata_chain_fixture.mjs')],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['checks'] == len(set(value['passed'])) == 16
    assert value['network_calls'] == value['browser_actions'] == 0
    assert value['real_owner_approval'] is False
