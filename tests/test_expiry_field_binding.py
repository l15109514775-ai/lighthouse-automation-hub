"""Owned DOM hydration changes, actual Chromium screenshots, no external calls."""
import json
import os
from pathlib import Path
import subprocess


def test_expiry_semantic_fields_survive_hidden_insertion_without_accepting_ambiguous_or_changed_sources(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', str(root / 'tests/expiry_field_binding_fixture.mjs'), str(tmp_path / 'expiry-field-binding')],
        cwd=root,
        env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root / 'compositor/runtime/browsers'),
             'TEMP': str(tmp_path), 'TMP': str(tmp_path), 'TMPDIR': str(tmp_path)},
        capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    value = json.loads(result.stdout)
    assert len(value['passed']) == len(set(value['passed'])) == 32
    assert value['external_requests'] == 0
    cases = {case['name']: case for case in value['summaries']}
    assert all(case['attempts'] == 1 for case in cases.values())
    assert cases['hidden-copies-inserted-before-every-field-read']['screenshots'] == 6
    assert cases['same-visible-field-replaced-after-every-real-screenshot']['decision'] == 'skipped_expired'
    assert cases['second-visible-own-full-time-even-in-footer-is-ambiguous']['decision'] == 'not_skipped'
    assert cases['actual-id-change-never-retries']['decision'] == 'not_skipped'
