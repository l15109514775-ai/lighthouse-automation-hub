"""Owned native recovery scenarios only; never a real site or account."""
import json
import os
from pathlib import Path
import signal
import subprocess
import pytest

@pytest.mark.timeout(100)
@pytest.mark.parametrize('scenario', ['normal_cache', 'restored_cache', 'wrong_account', 'protection',
    'expired_command_first_frame', 'missing_cache', 'wrong_cached_title', 'module_hash', 'submit_without_ticket',
    'cover_image', 'cover_video_rejected', 'cover_inspect', 'cover_hq_image', 'cover_hq_wrong_url', 'cover_hq_wrong_path',
    'cover_derived_image', 'cover_derived_wrong_provenance', 'cover_derived_wrong_output_hash'])
def test_recovered_editor_never_reselects_or_publishes_without_tickets(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(['node', str(root/'tests/browser_recovered_short_video_fixture.mjs'), str(tmp_path/'owned'), scenario],
        cwd=root, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root/'compositor/runtime/browsers')},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == 'posix')
    try:
        stdout, stderr = child.communicate(timeout=90)
    finally:
        if child.poll() is None:
            if os.name == 'posix': os.killpg(child.pid, signal.SIGTERM)
            else: child.terminate()
            child.wait(timeout=5)
    assert child.returncode == 0, stderr.decode('utf-8', 'replace')
    value = json.loads(next(x.removeprefix('FIXTURE_RESULT:') for x in stdout.decode().splitlines() if x.startswith('FIXTURE_RESULT:')))
    assert value['scenario'] == scenario
    assert value['selections'] == value['submit'] == value['real_uploads'] == value['external_network_calls'] == 0
    assert value['restore'] == (1 if scenario == 'restored_cache' else 0)
    assert value['cover_selections'] == (1 if scenario in ('cover_image', 'cover_hq_image', 'cover_derived_image') else 0)
    assert not value['submitted'] and not value['live_tested'] and not value['publication_authorized']
