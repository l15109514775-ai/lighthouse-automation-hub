"""Owned actual Chromium delayed manager DOM. No live platform or credentials."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario', ['async_draft', 'async_title', 'duplicate_draft', 'real_challenge', 'timeout'])
def test_saved_draft_navigation_waits_passively_for_unique_real_controls(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    env = {name: os.environ[name] for name in ('PATH', 'SystemRoot', 'SYSTEMROOT', 'SystemDrive', 'SYSTEMDRIVE',
        'ProgramData', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE',
        'TEMP', 'TMP', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'LANG', 'LC_ALL') if name in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(root/'compositor/runtime/browsers')
    result = subprocess.run(['node', str(root/'tests/saved_draft_navigation_wait_fixture.mjs'), str(tmp_path/'owned'), scenario],
        cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=35)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario'] == scenario and value['live_tested'] is False
    assert value['manager_navigations'] == 1
    assert value['file_selections'] == value['draft_saves'] == value['public_submissions'] == 0
    assert value['external_network_calls'] == value['unowned_http_responses'] == 0
