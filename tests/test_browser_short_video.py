"""Native bridge with owned routed HTML; no live account or platform upload."""
import json
import os
from pathlib import Path
import subprocess
import signal

import pytest


def resource_snapshot():
    result = {'cgroup': {}, 'zombie_processes': None}
    for name in ('pids.current', 'pids.max', 'pids.events', 'memory.current', 'memory.max', 'memory.events'):
        try:
            text = (Path('/sys/fs/cgroup')/name).read_text().strip()
            if all(c.isdigit() or c.isspace() or c.isalpha() or c == '_' for c in text):
                result['cgroup'][name] = text[:1000]
        except OSError:
            pass
    if os.name == 'posix':
        count = 0
        try:
            for item in Path('/proc').iterdir():
                if item.name.isdigit():
                    try:
                        if any(line.startswith('State:') and '\tZ' in line for line in (item/'status').read_text().splitlines()):
                            count += 1
                    except OSError:
                        pass
            result['zombie_processes'] = count
        except OSError:
            pass
    return result


@pytest.mark.parametrize('scenario', ['normal', 'clear', 'replace', 'two_files', 'no_completion', 'unsupported_control', 'bad_intent', 'submit_throws', 'submit_true', 'chooser_normal', 'chooser_clear', 'chooser_txt'])
def test_upload_only_bridge_preserves_native_causality_and_never_submits(tmp_path, scenario):
    root = Path(__file__).resolve().parents[1]
    resources = {'scenario': scenario, 'before': resource_snapshot()}
    proc = subprocess.Popen(['node', str(root/'tests/browser_short_video_fixture.mjs'), str(tmp_path/'owned'), scenario],
        cwd=root, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(root/'compositor/runtime/browsers')},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == 'posix')
    try:
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            if os.name == 'posix': os.killpg(proc.pid, signal.SIGTERM)
            else: proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == 'posix': os.killpg(proc.pid, signal.SIGKILL)
                else: proc.kill()
                proc.wait(timeout=3)
        resources['after'] = resource_snapshot()
        (tmp_path/'resource-snapshot.json').write_text(json.dumps(resources, indent=2)+'\n', 'utf-8')
    assert proc.returncode == 0, stderr.decode('utf-8', 'replace')
    lines = stdout.decode('utf-8').splitlines()
    result = json.loads(next(x.removeprefix('FIXTURE_RESULT:') for x in lines if x.startswith('FIXTURE_RESULT:')))
    assert result['scenario'] == scenario
    assert result['native_selections'] == (0 if scenario in ('bad_intent', 'chooser_txt') else 1)
    assert result['formal_submit_clicks'] == result['external_network_calls'] == result['real_uploads'] == 0
    if scenario.startswith('chooser_'):
        assert result['native_area_clicks'] == 1 and result['native_clicks'] >= 1
    else:
        assert result['native_clicks'] == 0
    assert not result['live_tested'] and not result['human_review'] and not result['publication_authorized']
    if scenario == 'submit_throws':
        assert result['submitted_after_close'] is None and result['submit_implemented'] is True
    elif scenario == 'submit_true':
        assert result['submitted_after_close'] is True and result['submit_implemented'] is True
