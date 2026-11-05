"""Owned real Chromium delayed controls; no real account or platform action."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario', ['async_animation', 'async_game', 'legacy_music', 'duplicate_option', 'option_timeout',
    'duplicate_source', 'source_timeout', 'protected_wait', 'unexpected_category', 'duplicate_category',
    'protected_source_blur', 'protected_tag_enter'])
def test_original_metadata_v5_controls_wait_bounded_and_remain_unique(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'SYSTEMROOT', 'SystemDrive', 'SYSTEMDRIVE',
        'ProgramData', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE',
        'TEMP', 'TMP', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'LANG', 'LC_ALL') if key in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(root/'compositor/runtime/browsers')
    result = subprocess.run(['node', str(root/'tests/metadata_v5_async_fixture.mjs'), str(tmp_path/'owned'), scenario],
        cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario'] == scenario and value['live_tested'] is False
    assert value['file_selections'] == value['public_submissions'] == value['draft_saves'] == 0
    assert value['external_network_calls'] == value['unowned_http_responses'] == 0
