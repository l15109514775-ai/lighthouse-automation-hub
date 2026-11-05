"""Owned routed Chromium cover completion only; no account or external post."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario', ['cover_complete', 'cover_protection', 'cover_ambiguous', 'cover_unknown_once'])
def test_native_cover_completion_is_bound_and_never_clicks_twice(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root/'tests/short_video_native_controls_fixture.mjs'),
        str(tmp_path/'owned'), scenario], cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=70,
        env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root/'compositor/runtime/browsers')})
    assert result.returncode == 0, result.stderr
    receipt = json.loads(next(line.removeprefix('FIXTURE_RESULT:') for line in result.stdout.splitlines()
                             if line.startswith('FIXTURE_RESULT:')))
    assert receipt['scenario'] == scenario
    assert receipt['external_network_calls'] == receipt['file_selections'] == receipt['public_submit'] == 0
    assert receipt['private_api_requests'] == receipt['storage_accesses'] == 0
    assert receipt['live_tested'] is receipt['real_owner_approval'] is False
    assert receipt['complete_clicks'] == (1 if scenario in ('cover_complete', 'cover_unknown_once') else 0)
    assert receipt['cancel_clicks'] == 0


@pytest.mark.parametrize('scenario', ['private_saved', 'private_unknown_once'])
def test_private_draft_native_confirmation_and_unknown_are_one_click_only(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root/'tests/short_video_native_controls_fixture.mjs'),
        str(tmp_path/'owned'), scenario], cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=70,
        env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root/'compositor/runtime/browsers')})
    assert result.returncode == 0, result.stderr
    receipt = json.loads(next(line.removeprefix('FIXTURE_RESULT:') for line in result.stdout.splitlines()
                             if line.startswith('FIXTURE_RESULT:')))
    assert receipt['scenario'] == scenario
    assert receipt['complete_clicks'] == receipt['draft_save_clicks'] == 1
    assert receipt['cancel_clicks'] == receipt['public_submit'] == receipt['file_selections'] == 0
    assert receipt['external_network_calls'] == receipt['private_api_requests'] == receipt['storage_accesses'] == 0
    assert receipt['live_tested'] is receipt['real_owner_approval'] is False
