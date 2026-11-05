"""Independent v4 PNG scenarios; preserve all existing browser fixtures."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario', ['v4_image', 'v4_large_image', 'v4_source_changed', 'v4_output_changed',
    'v4_wrong_output_sha', 'v4_mixed_v3_binding', 'v4_wrong_dimensions', 'v4_wrong_source_url'])
def test_recovered_v4_png_is_bound_and_cannot_reuse_v3_provenance(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root/'tests/browser_recovered_short_video_v4_fixture.mjs'),
        str(tmp_path/'owned'), scenario], cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=65,
        env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root/'compositor/runtime/browsers')})
    assert result.returncode == 0, result.stderr
    value = json.loads(next(line.removeprefix('FIXTURE_RESULT:') for line in result.stdout.splitlines()
                            if line.startswith('FIXTURE_RESULT:')))
    assert value['scenario'] == scenario
    assert value['video_selections'] == value['public_submit'] == value['external_network_calls'] == 0
    assert value['cover_selections'] == (1 if scenario in ('v4_image', 'v4_large_image') else 0)
    assert value['live_tested'] is value['real_normalizer_executed'] is value['real_owner_approval'] is False
