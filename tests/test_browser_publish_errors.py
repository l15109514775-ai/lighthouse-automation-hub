"""Pure Node error classification: no browser launch, credentials or profile I/O."""
import json
from pathlib import Path
import subprocess


def test_profile_in_use_is_launch_only_safe_and_does_not_override_existing_errors():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(['node', str(root / 'tests/browser_publish_errors_fixture.mjs')],
                          cwd=root, capture_output=True, timeout=15)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    result = json.loads(proc.stdout)
    assert len(result['passed']) == len(set(result['passed'])) == 24
    assert result['public_codes_verified'] >= 26
    assert result['browser_launches'] == result['profile_files_touched'] == result['external_calls'] == 0
