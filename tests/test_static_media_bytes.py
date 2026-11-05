"""Owned PNG/JPEG/WebP inputs; no platform, account, model or browser launch."""
import json
from pathlib import Path
import subprocess


def test_api_renderer_pure_media_validator_matches_preserved_legacy_behavior():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(['node', str(root / 'tests/static_media_bytes_fixture.mjs')],
        cwd=root, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    result = json.loads(proc.stdout)
    assert len(result['cases']) == len({v['name'] for v in result['cases']}) == 15
    assert all(v['passed'] is True for v in result['cases'])
    assert result['direct_imports'] == ['node:crypto', 'node:module']
    assert result['external_requests'] == result['browser_launches'] == result['model_calls'] == result['publish_requests'] == 0
