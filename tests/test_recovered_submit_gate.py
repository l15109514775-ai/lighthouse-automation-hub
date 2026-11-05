"""Exercise production admission using owned VM-linked filesystem observations."""
import json
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario', ['normal_once', 'concurrent_once', 'replay_new_recovery',
    'expired', 'missing_approval', 'wrong_account', 'wrong_attempt', 'wrong_revision', 'wrong_metadata',
    'wrong_uploadref', 'reservation_denied', 'stale_approval_hash', 'untrusted_owner', 'writable_mount',
    'symlink_ticket', 'artifact_changed', 'unsafe_output', 'fsync_fail'])
def test_recovered_submit_gate_requires_root_reservation_and_durable_one_use_intent(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', '--experimental-vm-modules',
        str(root/'tests/recovered_submit_gate_fixture.mjs'), str(tmp_path/'owned'), scenario],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip())
    assert receipt['scenario'] == scenario and receipt['passed'] is True
    assert receipt['real_network_calls'] == receipt['native_clicks'] == 0
