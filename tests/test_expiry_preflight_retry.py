"""Real Chromium, owned HTML and PNGs; no platform, model or publishing call."""
import json
import os
from pathlib import Path
import subprocess


def test_expiry_retry_reacquires_all_proofs_only_for_stable_identity_and_geometric_changes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/expiry_preflight_retry_fixture.mjs'), str(tmp_path / 'expiry-retry')],
        cwd=root, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root / 'compositor/runtime/browsers'),
                       'TEMP': str(tmp_path), 'TMP': str(tmp_path), 'TMPDIR': str(tmp_path)},
        capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    value = json.loads(result.stdout)
    assert len(value['passed']) == 18 and len(set(value['passed'])) == 18
    assert value['external_requests'] == 0
    cases = {x['name']: x for x in value['summaries']}
    assert cases['stable-source-one-attempt-six-real-clips']['screenshots'] == 6
    assert cases['geometry-change-first-target-then-six-fresh-clips']['screenshots'] == 7
    assert cases['moving-every-attempt-never-gets-fourth-attempt']['attempts'] == 3
    assert cases['moving-every-attempt-never-gets-fourth-attempt']['decision'] == 'not_skipped'
