"""Owned, offline deployment mutation and failure recovery fixtures.

No subprocess, network, OS UID changes, production data or live deletion.
"""
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
import re
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import tomllib
import uuid

import pytest
from historical_artifacts import historical_member

FILE = Path(__file__).resolve().parents[2] / 'scripts/ops/deploy_youtube_acceptance_pilot_v1.py'
SPEC = importlib.util.spec_from_file_location('youtube_pilot_deploy_owned', FILE)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)
COMPOSE = FILE.parents[2] / 'compose.yaml'
HUB = b'''# owned fixture\r\nyoutube_download_enabled = false # deliberate hold\r\npolicy_version = "machine-tweet-v3-api"\r\n[budgets.x]\r\ndaily_usd = 0.5\r\nmonthly_usd = 5\r\n'''
POLICY = b'''version = "youtube-download-policy-v1"\npolicy_id = "youtube-download-auth-v1"\nactivation_at = "2026-10-31T12:00:00Z"\nacceptance_video_ids = ["0H6_bTvWeDQ"] # previous short\nowner_permission_reference = "owned-rights"\nacquisition_authorization_reference = "owned-download-consent"\nsession_profile = "youtube"\nsession_authorized_at = "2026-10-31T13:00:00Z"\nmax_bytes = 3800000000\nmax_height = 720\ntimeout_seconds = 21600\nmax_new_replays_per_day = 4\n'''


def normalized():
    common = {'image': 'lighthouse-hub:0.1.0', 'restart': 'unless-stopped',
              'environment': {'ONLY_OWNED_FIXTURE': 'not-a-real-secret'},
              'logging': {'driver': 'local', 'options': {'max-size': '10m', 'max-file': '3'}},
              'volumes': [{'type': 'bind', 'source': '/opt/lighthouse-hub/data',
                           'target': '/opt/lighthouse-hub/data', 'bind': {'create_host_path': True}}]}
    services = {n: deepcopy(common) for n in ('api', 'scheduler', 'tweet-worker', 'system-worker', 'video-worker')}
    services['api']['ports'] = [{'host_ip': '127.0.0.1', 'target': 8080, 'published': '8080'}]
    services['video-worker']['volumes'].append({'type': 'bind', 'source': '/opt/lighthouse-hub/data/browser-profiles/youtube',
                                               'target': '/opt/lighthouse-hub/data/browser-profiles/youtube', 'read_only': True})
    return {'name': 'lighthouse-hub', 'services': services, 'x-runtime': deepcopy(common)}


def with_pilot(before):
    after = deepcopy(before)
    for name in ('api', 'system-worker', 'video-worker'):
        after['services'][name]['volumes'] += [pilot.source_volume(n) for n in pilot.SOURCES]
    return after


@pytest.fixture
def posix_paths(monkeypatch):
    monkeypatch.setattr(pilot, 'ROOT', PurePosixPath('/opt/lighthouse-hub'))
    monkeypatch.setattr(pilot, 'INSTALL', PurePosixPath('/usr/local/lib/lighthouse-video-pilot-v1'))


def test_hub_only_changes_two_booleans_and_preserves_bom_crlf_comment():
    raw = b'\xef\xbb\xbf' + HUB
    out = pilot.mutate_hub(raw)
    before = tomllib.loads(raw.decode('utf-8-sig'))
    assert tomllib.loads(out.decode('utf-8-sig')) == {**before, 'youtube_download_enabled': True,
                                                    'youtube_download_acceptance_only': True}
    assert out.startswith(b'\xef\xbb\xbf') and b'true # deliberate hold\r\n' in out
    assert out.replace(b'\r\n', b'').count(b'\n') == 0
    assert out.endswith(b'monthly_usd = 5\r\n')


def test_hub_updates_existing_top_level_switch_without_changing_nested_key():
    raw = b'youtube_download_enabled=false\nyoutube_download_acceptance_only=false\n[extra]\nyoutube_download_acceptance_only="unchanged"\n'
    out = tomllib.loads(pilot.mutate_hub(raw).decode())
    assert out['youtube_download_acceptance_only'] is True
    assert out['extra'] == {'youtube_download_acceptance_only': 'unchanged'}


def test_policy_only_replaces_whitelist_preserving_real_activation_rights_and_caps():
    before = tomllib.loads(POLICY.decode())
    out = pilot.mutate_policy(POLICY)
    assert tomllib.loads(out.decode()) == {**before, 'acceptance_video_ids': ['9T5tn1WGlJg']}
    assert b'activation_at = "2026-10-31T12:00:00Z"' in out
    assert b'max_bytes = 3800000000' in out and b'# previous short' in out


@pytest.mark.parametrize('invalid', ['["one"]', '["0H6_bTvWeDQ","9T5tn1WGlJg"]', 'true', '42'])
def test_policy_rejects_unexpected_existing_whitelist(invalid):
    raw = POLICY.replace(b'["0H6_bTvWeDQ"]', invalid.encode())
    with pytest.raises(AssertionError):
        pilot.mutate_policy(raw)


def test_canonical_text_only_adds_six_read_only_binds_and_preserves_profile(posix_paths):
    raw = COMPOSE.read_bytes()
    out = pilot.mutate_compose(raw)
    assert out.count(b'/usr/local/lib/lighthouse-video-pilot-v1/config.py') == 3
    assert out.count(b'/usr/local/lib/lighthouse-video-pilot-v1/youtube_download.py') == 3
    assert out.count(b'"read_only":true') == 6
    assert out.count(b'"create_host_path":false') == 6
    profile = b'./data/browser-profiles/youtube:/opt/lighthouse-hub/data/browser-profiles/youtube:ro'
    assert raw.count(profile) >= 1 and out.count(profile) == raw.count(profile)
    for name in ('scheduler', 'tweet-worker', 'replay-acquisition-worker'):
        def block(text):
            segment = text.split(('  '+name+':\n').encode(), 1)[1]
            return re.split(rb'\n  [A-Za-z0-9_-]+:\n', segment, maxsplit=1)[0]
        assert block(out) == block(raw)
    # Every inherited original mount remains, including all five for each of
    # api/system/video and the independent authenticated YouTube profile.
    assert out.count(b'./config:/opt/lighthouse-hub/config:ro') == raw.count(b'./config:/opt/lighthouse-hub/config:ro') + 2


def test_second_pilot_mutation_refuses_duplicate_targets(posix_paths):
    with pytest.raises(AssertionError):
        pilot.mutate_compose(pilot.mutate_compose(COMPOSE.read_bytes()))


def test_normalized_compose_only_delta_accepts_three_pairs(posix_paths):
    before = normalized()
    after = with_pilot(before)
    pilot.assert_compose_delta(before, after)
    assert after['services']['video-worker']['volumes'][1]['read_only'] is True


@pytest.mark.parametrize('field', ['image', 'restart', 'environment', 'ports', 'logging', 'old_volume', 'scheduler'])
def test_effective_delta_rejects_any_unrelated_runtime_change(posix_paths, field):
    before = normalized()
    after = with_pilot(before)
    if field == 'old_volume':
        after['services']['video-worker']['volumes'][0]['source'] = '/other'
    elif field == 'scheduler':
        after['services']['scheduler']['restart'] = 'no'
    else:
        after['services']['api'][field] = {'unexpected': 'owned-fixture'}
    with pytest.raises(AssertionError):
        pilot.assert_compose_delta(before, after)


@pytest.mark.parametrize('field,value', [('read_only', False), ('source', '/home/operator/unpinned.py'),
                                         ('bind', {'create_host_path': True})])
def test_pilot_mount_fail_closed_on_writeable_or_unpinned_source(posix_paths, field, value):
    before = normalized()
    after = with_pilot(before)
    after['services']['api']['volumes'][-1][field] = value
    with pytest.raises(AssertionError):
        pilot.assert_compose_delta(before, after)


@pytest.fixture
def owned_attestation(tmp_path, monkeypatch):
    ev = tmp_path/'owned-evidence'
    evidence = ev/('youtube-pilot-overlay-v1-'+str(uuid.uuid4()))
    evidence.mkdir(parents=True)
    repo = FILE.parents[2]
    for name in pilot.SOURCES:
        target = evidence/'overlay'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(historical_member('historical-deployment-source-inputs.tar',name,pilot.SOURCES[name]))
    groups = {'tests/test_youtube_download.py': 71, 'tests/test_youtube_download_acceptance_only.py': 15}
    body = {'version': 'youtube-pilot-overlay-ubuntu-tests-v1', 'image': pilot.TEST_IMAGE,
            'tests': 86, 'failures': 0, 'errors': 0, 'skipped': 0, 'exit_code': 0, 'oom_killed': False,
            'full_suite_passed': True, 'network': 'none', 'application_data_mounted': False,
            'whole_tree_deployed': False, 'runtime_readonly_overlays_only': True,
            'runtime_matches_production': True, 'no_pid_or_oom_denial': True,
            'manifest': {'base_image': pilot.TEST_IMAGE, 'expected_total_tests': 86,
                         'expected_test_counts': groups, 'files': {n: {'sha256': d} for n,d in pilot.SOURCES.items()}},
            'groups': groups, 'real_uploads': 0, 'publications': 0}
    report = evidence/'test-attestation.json'
    report.write_text(json.dumps(body))
    # Deployed pointer path, never a manually transcribed attempt UUID.
    (ev/'youtube-pilot-current-test.json').write_text(json.dumps({'evidence': str(evidence)}))
    monkeypatch.setattr(pilot, 'EV', ev)
    return ev, evidence, report, body


def test_ubuntu_attestation_and_two_exact_source_hashes(owned_attestation):
    _, _, _, _ = owned_attestation
    sources, metadata = pilot.read_attestation()
    assert set(sources)==set(pilot.SOURCES) and metadata['tests']==86
    assert all(pilot.sha(data)==pilot.SOURCES[name] for name,data in sources.items())


@pytest.mark.parametrize('key,value', [('tests',85), ('oom_killed',True), ('application_data_mounted',True), ('real_uploads',1)])
def test_ubuntu_attestation_rejects_wrong_or_unsafe_execution(owned_attestation, key, value):
    _, _, report, body = owned_attestation
    body[key]=value
    report.write_text(json.dumps(body))
    with pytest.raises(AssertionError):
        pilot.read_attestation()


def test_ubuntu_attestation_rejects_changed_runtime_source(owned_attestation):
    _, evidence, _, _ = owned_attestation
    source=evidence/'overlay/services/hub/config.py'
    source.write_bytes(source.read_bytes()+b'\n# owned mutation\n')
    with pytest.raises(AssertionError):
        pilot.read_attestation()


@pytest.fixture
def owned_deploy(tmp_path, monkeypatch):
    root = tmp_path / 'hub'
    (root/'config').mkdir(parents=True)
    originals = {root/'compose.yaml': COMPOSE.read_bytes(), root/'config/hub.toml': HUB,
                 root/'config/youtube-download-policy.toml': POLICY}
    for path, data in originals.items():
        path.write_bytes(data)
    monkeypatch.setattr(pilot, 'ROOT', root)
    monkeypatch.setattr(pilot, 'INSTALL', tmp_path/'install')
    monkeypatch.setattr(pilot, 'read_attestation', lambda: ({}, {'tests': 86, 'owned_fixture': True}))
    monkeypatch.setattr(pilot, 'install_sources', lambda sources: None)
    monkeypatch.setattr(pilot, 'worker_locks', lambda: nullcontext())
    before = normalized()
    after = with_pilot(before)
    monkeypatch.setattr(pilot, 'parsed_compose', lambda path: before if path.read_bytes()==originals[root/'compose.yaml'] else after)
    monkeypatch.setattr(pilot, 'run', lambda *a, **k: pilot.CBC+'\n')
    state = {'active_video_jobs': [], 'runnable_download_jobs': [], 'short_record_hashes': {'owned': 'unchanged'},
             'target_download_jobs': 0, 'download_job_count': 1}
    monkeypatch.setattr(pilot, 'database_state', lambda **kw: deepcopy(state))
    monkeypatch.setattr(pilot, 'verify_services', lambda policy: {'owned': 'probe'})
    monkeypatch.setattr(pilot, 'wait_ready', lambda: {'status': 'ready', 'stale_services': []})
    calls = []
    monkeypatch.setattr(pilot, 'compose', lambda *a, **k: calls.append(a) or '')
    writes = []
    def write(path, data, **kw):
        writes.append((path, data))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(pilot, 'atomic', write)
    return root, originals, calls, writes, write


def test_default_plan_does_not_stop_or_write_persistent_configs(owned_deploy):
    root, originals, calls, writes, _ = owned_deploy
    assert pilot.deploy(SimpleNamespace(deploy=False)) == 0
    assert not calls
    assert all(path.read_bytes()==data for path,data in originals.items())
    assert len(writes)==1 and writes[0][0].name.startswith('.compose-video-pilot-')
    assert not writes[0][0].exists()


def test_stop_command_partial_failure_still_restarts_original_services(owned_deploy, monkeypatch):
    root, originals, calls, _, _ = owned_deploy
    first = True
    def compose(*args, **kwargs):
        nonlocal first
        calls.append(args)
        if args[0]=='stop' and first:
            first = False
            raise RuntimeError('OWNED_PARTIAL_STOP')
        return ''
    monkeypatch.setattr(pilot, 'compose', compose)
    assert pilot.deploy(SimpleNamespace(deploy=True)) == 2
    assert all(path.read_bytes()==data for path,data in originals.items())
    assert len([c for c in calls if c[0]=='up']) == 1
    report = next((root/'backups/video-pilot-deploy').glob('*/deployment-report.json'))
    assert json.loads(report.read_bytes())['rollback_verified'] is True


def test_second_config_write_failure_restores_all_three_originals(owned_deploy, monkeypatch):
    root, originals, calls, _, write = owned_deploy
    failed = False
    def atomic(path, data, **kwargs):
        nonlocal failed
        if path==root/'config/hub.toml' and data!=originals[path] and not failed:
            failed = True
            raise OSError('OWNED_RENAME_FAILURE')
        write(path, data, **kwargs)
    monkeypatch.setattr(pilot, 'atomic', atomic)
    assert pilot.deploy(SimpleNamespace(deploy=True)) == 2
    assert failed and all(path.read_bytes()==data for path,data in originals.items())
    assert len([c for c in calls if c[0]=='up']) == 1
    report = json.loads(next((root/'backups/video-pilot-deploy').glob('*/deployment-report.json')).read_bytes())
    assert report['rollback_verified'] is True and report['database_or_media_restored_or_deleted'] is False


def test_success_only_restarts_three_and_never_queues_target(owned_deploy):
    root, originals, calls, _, _ = owned_deploy
    assert pilot.deploy(SimpleNamespace(deploy=True)) == 0
    assert [c[0] for c in calls] == ['stop', 'up']
    assert calls[0][1:] == ('api', 'system-worker', 'video-worker')
    assert '--no-build' in calls[1] and calls[1][-3:] == ('api', 'system-worker', 'video-worker')
    manifest = json.loads((pilot.INSTALL/'manifest.json').read_bytes())
    assert manifest['pilot_target_job_queued_by_script'] is False
    assert manifest['full_automatic_download_or_bilibili_publish_enabled'] is False
    assert tomllib.loads((root/'config/hub.toml').read_text())['youtube_download_acceptance_only'] is True
    assert tomllib.loads((root/'config/youtube-download-policy.toml').read_text())['activation_at'] == '2026-10-31T12:00:00Z'


def test_historical_pilot_attestation_does_not_authorize_current_config(owned_attestation):
    _,evidence,_,_=owned_attestation
    name='services/hub/config.py';raw=(FILE.parents[2]/name).read_bytes()
    assert pilot.sha(raw)!=pilot.SOURCES[name]
    (evidence/'overlay'/name).write_bytes(raw)
    with pytest.raises(AssertionError):pilot.read_attestation()
