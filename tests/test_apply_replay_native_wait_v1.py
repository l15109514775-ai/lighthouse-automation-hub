"""Pure host boundary tests: no Docker, network, live DB, account or mutation."""
from copy import deepcopy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import uuid

import pytest

from scripts.ops import apply_replay_native_wait_v1 as m


def maps():
    old = {**{name: 'a'*64 for name in m.PRODUCTION},
           **{f'owned/unchanged-{index}.py': 'b'*64 for index in range(41)}}
    return old, {**old, **m.PRODUCTION}


def test_exact_45_source_map_changes_only_four_frozen_sources():
    old, new = maps();saved = deepcopy((old, new))
    m.validate_source_change(old, new)
    assert (old, new) == saved


@pytest.mark.parametrize('case', ['missing', 'fifth_change', 'extra_source', 'wrong_pin', 'unchanged_repair'])
def test_source_map_rejects_unqualified_or_incomplete_repair(case):
    old, new = maps();name = next(iter(m.PRODUCTION))
    if case == 'missing':new.pop(name)
    elif case == 'fifth_change':new['owned/unchanged-0.py'] = 'c'*64
    elif case == 'extra_source':new['owned/fifth.py'] = 'd'*64
    elif case == 'wrong_pin':new[name] = '0'*64
    else:new[name] = old[name]
    with pytest.raises(AssertionError):m.validate_source_change(old, new)


def config():
    root = PurePosixPath('/owned/hub')
    value = {'services': {'video-worker': {
        'image': 'owned-pinned-image', 'command': ['python', '-m', 'owned-existing-entry'],
        'environment': {'HOME': '/tmp/highest/home', 'OWNED_TEST_ONLY': 'unchanged'},
        'tmpfs': ['/tmp/highest/home:rw,size=8m', '/tmp/highest/config:rw,size=16m', '/tmp/highest/cache:rw,size=32m'],
        'volumes': [{'source': str(Path('/old')/name), 'target': str(root/name), 'read_only': True} for name in m.PRODUCTION]
                   + [{'source': '/owned/profile', 'target': '/owned/profile', 'read_only': False}],
        'restart': 'unless-stopped', 'ports': []},
        **{name: {'owned': name, 'unchanged': True} for name in ('api', 'scheduler', 'tweet-worker', 'events')}}}
    calls = []
    def other(before, after):
        calls.append(True)
        assert all(before['services'][name] == after['services'][name] for name in before['services'] if name != 'video-worker')
    return value, SimpleNamespace(ROOT=root, assert_other_services=other), calls


def test_candidate_replaces_four_existing_ro_source_paths_and_nothing_else():
    before, helper, calls = config();saved = deepcopy(before);target = PurePosixPath('/new/frozen')
    after = m.candidate_config(before, target, helper)
    assert before == saved and calls == [True]
    reverse = deepcopy(after)
    for old, current in zip(saved['services']['video-worker']['volumes'], reverse['services']['video-worker']['volumes']):
        if old['target'] in {str(helper.ROOT/name) for name in m.PRODUCTION}:
            assert current['source'] == str(target/old['target'][len(str(helper.ROOT))+1:])
            current['source'] = old['source']
    assert reverse == saved


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'writable'])
def test_candidate_rejects_missing_duplicate_or_writable_original_mount(case):
    before, helper, calls = config();volumes = before['services']['video-worker']['volumes']
    if case == 'missing':volumes.pop(0)
    elif case == 'duplicate':volumes.append(deepcopy(volumes[0]))
    else:volumes[0]['read_only'] = False
    with pytest.raises(AssertionError):m.candidate_config(before, PurePosixPath('/new'), helper)
    assert calls == []


def uploaded():
    state = {'state': 'uploaded', 'version': 'replay-highest-video-website-v2', 'attempt_id': str(uuid.uuid4()),
             'public_submission_approved': False, 'human_review': False}
    result = {'replay_highest_video_web_v2': state, 'publish_performed': False}
    row = {'job_id': m.PILOT, 'job_type': 'replay_highest_video_publish_v2', 'source_platform': 'youtube',
           'source_id': m.SOURCE, 'payload': {'source_id': m.SOURCE, 'download_job_id': m.DOWNLOAD},
           'state': 'awaiting_review', 'attempts': 2, 'error_code': 'REPLAY_BROWSER_METADATA_PREPARATION_FAILED',
           'lease_owner': None, 'lease_until': None, 'external_publish_id': None, 'result': result}
    return row, result, [], {'video-selection.json'}


def test_only_uploaded_second_attempt_without_publication_or_submit_intent_is_admitted():
    args = uploaded();saved = deepcopy(args)
    assert m.job_payload_safe(*args) is None and args == saved


@pytest.mark.parametrize('case', ['lease', 'publication', 'submit_intent', 'wrong_checkpoint', 'prior_submit'])
def test_uploaded_job_guard_rejects_lease_publication_or_unsafe_history(case):
    row, result, pubs, intents = uploaded();state = result['replay_highest_video_web_v2']
    if case == 'lease':row['lease_until'] = 9999999999
    elif case == 'publication':pubs.append({'state': 'unknown'})
    elif case == 'submit_intent':intents.add('public-submit.json')
    elif case == 'wrong_checkpoint':state['state'] = 'submit_unknown'
    else:state['machine_acceptance'] = {'owned': 'prior send evidence'}
    with pytest.raises(AssertionError):m.job_payload_safe(row, result, pubs, intents)


def test_loader_executes_only_once_captured_hash_verified_bytes(tmp_path, monkeypatch):
    file = tmp_path/'owned.py';raw = b'OWNED_VALUE = 7\n';file.write_bytes(raw)
    monkeypatch.setattr(m, 'OPERATOR_SHA', m.sha(raw))
    original = Path.read_bytes;calls = []
    def change_after_read(path):
        body = original(path)
        if path == file:
            calls.append(True);file.write_bytes(b'raise RuntimeError("must not execute replacement")\n')
        return body
    monkeypatch.setattr(Path, 'read_bytes', change_after_read)
    assert m.load_operator(file).OWNED_VALUE == 7 and calls == [True]


def test_loader_rejects_unverified_bytes_before_execution(tmp_path):
    file = tmp_path/'owned.py';file.write_bytes(b'raise RuntimeError("must not execute")\n')
    with pytest.raises(AssertionError):m.load_operator(file)


def qualified():
    _, runtime = maps()
    files = {name: {'sha256': digest, 'bytes': 1} for name, digest in runtime.items()}
    files.update({f'tests/owned-{i}.py': {'sha256': 'e'*64, 'bytes': 1} for i in range(47)})
    manifest = {'parent_archive_sha256': m.PARENT, 'parent_expected_total_tests': 21,
                'python_source_commit': 'c8986648255244274bbfd1d2a882d5e06cf5681d',
                'native_source_commit': '3b48ec2cc092a13273ca6f7a2809319478d42d49',
                'natural_sources_installed': False, 'native_freshness_seconds_unchanged': 300,
                'source_current_full_sha_required_each_call': True, 'production_runtime_changed_files': list(m.PRODUCTION),
                'runtime_source_sha256': runtime, 'files': files}
    observed = {name: record['sha256'] for name, record in files.items()}
    return {'manifest': manifest, 'mounted_sources_match_manifest': True, 'host_source_sha256_unchanged': True,
            'timed_out': False, 'groups': deepcopy(m.GROUPS),
            'mounted_source_sha256_before': observed, 'mounted_source_sha256_after': deepcopy(observed)}


def test_qualification_extends_exact_existing_gate_and_always_removes_temporary_spec(tmp_path):
    value = qualified();seen = [];specs = {'owned-existing': ('unchanged',)}
    def gate(path, digest, kind, helper):
        seen.append((path, digest, kind, helper, specs[kind]));return value, {'owned': 'already-verified-base-gate'}
    helper = SimpleNamespace(SPECS=specs, gate=gate)
    got, reference = m.qualification(tmp_path/'test-attestation.json', '0'*64, helper, 'owned-deployer')
    assert got is value and reference == {'owned': 'already-verified-base-gate'}
    assert seen[0][2] == 'native_wait' and seen[0][-1][-3:] == (68, 92, m.ARCHIVE)
    assert specs == {'owned-existing': ('unchanged',)} and len(value['manifest']['runtime_source_sha256']) == 45


def test_qualification_rejects_false_or_changed_mounted_evidence_not_only_pass_boolean(tmp_path):
    for change in ('mounted', 'timeout', 'source_commit', 'extra_changed', 'freshness', 'full_sha'):
        value = qualified()
        if change == 'mounted':value['mounted_source_sha256_after'][next(iter(m.PRODUCTION))] = '0'*64
        elif change == 'timeout':value['timed_out'] = True
        elif change == 'source_commit':value['manifest']['python_source_commit'] = 'wrong-owned-freeze'
        elif change == 'extra_changed':value['manifest']['production_runtime_changed_files'].append('owned/fifth.py')
        elif change == 'freshness':value['manifest']['native_freshness_seconds_unchanged'] = 999
        else:value['manifest']['source_current_full_sha_required_each_call'] = False
        helper = SimpleNamespace(SPECS={}, gate=lambda *args:(value, {}))
        with pytest.raises(AssertionError):m.qualification(tmp_path/'test-attestation.json', '0'*64, helper, None)
        assert helper.SPECS == {}
