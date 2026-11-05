"""Owned Chromium and FFmpeg only; no Bilibili, credentials or public submit."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario', ['ordered_save', 'existing_draft', 'bad_chooser', 'reversed_rows',
    'file_changed', 'save_unknown', 'wrong_uid', 'challenge', 'restore_saved', 'restore_duplicate'])
def test_two_part_native_private_boundaries_on_owned_chromium(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'SYSTEMROOT', 'SystemDrive', 'SYSTEMDRIVE',
        'ProgramData', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE',
        'TEMP', 'TMP', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'LANG', 'LC_ALL') if key in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(root / 'compositor/runtime/browsers')
    result = subprocess.run(['node', str(root / 'tests/multipart_private_v4_fixture.mjs'), scenario, str(tmp_path / 'owned')],
        cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=75)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout.strip().splitlines()[-1])
    assert value['scenario'] == scenario and value['owned_fixture'] is True and value['real_ffmpeg_clips'] is True
    assert value['public_clicks'] == value['external_requests'] == 0
    assert value['native_multipart_verified'] is False
