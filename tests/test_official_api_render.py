"""Render API-authoritative owned responses without generating source screenshots."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.timeout(300)
def test_official_api_renderer_reuses_template_but_requires_api_and_model_evidence(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', str(root / 'tests/official_api_render_fixture.mjs'), str(tmp_path / 'official-api-render')],
        cwd=root,
        env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root / 'compositor/runtime/browsers'),
             'TEMP': str(tmp_path), 'TMP': str(tmp_path), 'TMPDIR': str(tmp_path)},
        capture_output=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    record = json.loads(result.stdout)
    assert len(record['passed']) == len(set(record['passed'])) == 48
    assert record['external_calls'] == record['model_calls'] == record['publish_requests'] == 0
    assert record['human_review'] is False and record['publication_authorized'] is False
    report = json.loads(Path(record['report_file']).read_text(encoding='utf-8'))
    assert report['version'] == 'machine-render-api-v1'
    assert report['source_screenshot'] is None
    assert report['upstream_human_publishable'] is False
    assert len(record['orig_report_files']) == 2
    for file in record['orig_report_files']:
        rendition_report = json.loads(Path(file).read_text(encoding='utf-8'))
        assert rendition_report['machine_renderable'] is True
        assert rendition_report['publication_authorized'] is False
        assert rendition_report['source_screenshot'] is None
        assert rendition_report['upstream_human_publishable'] is False
