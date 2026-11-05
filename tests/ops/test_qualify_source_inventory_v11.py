"""Owned v11 qualification boundaries; no Docker, server or platform requests."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


wrapper = load('owned_v11_qualification_wrapper', 'scripts/ops/qualify_source_inventory_v11.py')
ret = load('owned_v11_retention_parser', 'scripts/ops/qualify_replay_parts_retention_v5.py')


@pytest.fixture
def op():
    return wrapper.operator()


def xml(group, count):
    return ('<testsuites><testsuite>' + ''.join('<testcase classname="' + group + '" name="owned_' + str(i) + '"/>' for i in range(count)) + '</testsuite></testsuites>').encode()


@pytest.fixture
def inputs(op):
    fixed = set(ret.NODE_SOURCES) | op.PRODUCTION
    files = {f'services/hub/owned_{i:03}.py': {'bytes': 1, 'sha256': op.sha(b'a')} for i in range(141-len(fixed))}
    files.update({n: {'bytes': 1, 'sha256': op.sha(b'a')} for n in fixed})
    files.update({f'tests/test_owned_{i:03}.py': {'bytes': 1, 'sha256': op.sha(b'a')} for i in range(914-len(files))})
    old_manifest = {'source_commit': 'a'*40, 'files': files}
    px, rx = xml('tests.test_owned_parent', 45), xml(ret.GROUP, 26)
    parent = {'version': 'replay-editor-ubuntu-v10', 'passed': True, 'tests': 45, 'image': op.BASE,
              'junit_sha256': op.sha(px), 'test_groups': {'tests.test_owned_parent': 45}, 'source_manifest': old_manifest}
    raw = op.wire(parent); op.PARENT_SHA = op.sha(raw)
    old = {'version': ret.VERSION, 'full_suite_passed': True, 'image': op.BASE, 'qualified_parent_sha256': op.PARENT_SHA,
           'junit_sha256': op.sha(rx), 'test_groups': {ret.GROUP: 26}, 'source_files': op.runtime_sources(ret, old_manifest)}
    oldraw = op.wire(old); op.OLD26_SHA = op.sha(oldraw)
    blobs = {wrapper.WHOLE: b'owned whole delta', op.RETENTION_PARSER: (ROOT/op.RETENTION_PARSER).read_bytes(),
             op.RUNNER: (ROOT/op.RUNNER).read_bytes(), 'tests/test_owned_editor.py': b'owned fixture'}
    op.FROZEN_RUNTIME_SHA256 = {wrapper.WHOLE: op.sha(blobs[wrapper.WHOLE])}
    current = deepcopy(old_manifest)
    for name, value in blobs.items(): current['files'][name] = {'bytes': len(value), 'sha256': op.sha(value)}
    current.update(source_commit='b'*40, expected_test_count=2, test_selections=['tests/test_owned_editor.py'])
    manifest = {'version': 'source-inventory-candidate-v11', 'base_image': op.BASE, 'parent_sha256': op.PARENT_SHA,
                'parent_retention_sha256': op.OLD26_SHA, 'base_source_manifest': old_manifest, 'source_manifest': current,
                'source_commit': 'b'*40, 'production_runtime_changed_files': sorted(op.PRODUCTION),
                'delta_sha256': {n: op.sha(v) for n, v in blobs.items()},
                'test_groups': {'tests.test_owned_editor': 2}, 'test_selections': current['test_selections'][:]}
    return parent, raw, oldraw, px, rx, manifest


def test_parent_helper_byte_change_refuses_before_execution():
    raw = (ROOT/'scripts/ops/qualify_download_access_v9.py').read_bytes()
    with pytest.raises(ValueError, match='PARENT_BYTES_CHANGED'): wrapper.derive(raw+b'\n')


def test_each_adaptation_requires_exactly_one_match(monkeypatch):
    raw = (ROOT/'scripts/ops/qualify_download_access_v9.py').read_bytes()
    assert len(wrapper.REPLACEMENTS) == len({a for a, _ in wrapper.REPLACEMENTS})
    for old, _ in wrapper.REPLACEMENTS:
        changed = raw + ('\n# '+old+'\n').encode()
        monkeypatch.setattr(wrapper, 'PARENT_HELPER_SHA', wrapper.hashlib.sha256(changed).hexdigest())
        with pytest.raises(ValueError, match='EXACT_ONE'): wrapper.derive(changed)


def test_scope_and_current_parent_are_whole_only(op):
    assert op.PRODUCTION == {'adapters/ytdlp_whole_v4.py'}
    assert op.CURRENT == wrapper.CURRENT and op.BASE == wrapper.BASE
    assert op.PARENT == wrapper.PARENT and op.OLD26 == wrapper.OLD26
    assert op.RUNNER.endswith('qualify_source_inventory_v11.py')


def test_actual_v9_45_and26_parent_required(op, inputs):
    parent, raw, oldraw, px, rx, _ = inputs
    assert op.validate_parent(ret, raw, oldraw, px, rx)[0] == parent
    changed = {**parent, 'tests': 141}; bad = op.wire(changed); op.PARENT_SHA = op.sha(bad)
    with pytest.raises(AssertionError): op.validate_parent(ret, bad, oldraw, px, rx)


def test_parent_manifest_914_and_runtime141_are_preserved(op, inputs):
    parent, _, _, _, _, manifest = inputs
    assert len(op.validate_candidate(ret, manifest, parent)[1]) == 141
    assert len(manifest['base_source_manifest']['files']) == 914
    bad = deepcopy(manifest); bad['base_source_manifest']['source_commit'] = 'f'*40
    with pytest.raises(AssertionError): op.validate_candidate(ret, bad, parent)


@pytest.mark.parametrize('name', ['services/hub/cloudbase_backup_runtime.py', 'config/hub.toml', 'scripts/ops/deploy_source_inventory_v11.py'])
def test_unrelated_runtime_configuration_or_deployer_payload_refused(op, inputs, name):
    parent, _, _, _, _, manifest = inputs
    bad = deepcopy(manifest); bad['delta_sha256'][name] = op.sha(b'bad')
    bad['source_manifest']['files'][name] = {'bytes': 3, 'sha256': op.sha(b'bad')}
    with pytest.raises(AssertionError): op.validate_candidate(ret, bad, parent)


def test_missing_or_wrong_whole_is_refused(op, inputs):
    parent, _, _, _, _, manifest = inputs
    for mode in ('missing', 'wrong'):
        bad = deepcopy(manifest)
        if mode == 'missing': bad['delta_sha256'].pop(wrapper.WHOLE)
        else: bad['delta_sha256'][wrapper.WHOLE] = 'f'*64
        with pytest.raises(AssertionError): op.validate_candidate(ret, bad, parent)


def test_ten_to_eleven_layers_requires_same_config_and_platform(op):
    before = {'Id': op.BASE, 'RootFS': {'Layers': [str(i) for i in range(10)]}, 'Config': {'User': '10001:10001'},
              'Os': 'linux', 'Architecture': 'amd64', 'Variant': ''}
    after = deepcopy(before); after['RootFS']['Layers'].append('owned delta')
    op.validate_image_lineage(before, after)
    for key in ('layers', 'config', 'platform'):
        bad = deepcopy(after)
        if key == 'layers': bad['RootFS']['Layers'].append('extra')
        elif key == 'config': bad['Config']['User'] = '0'
        else: bad['Architecture'] = 'arm64'
        with pytest.raises(AssertionError): op.validate_image_lineage(before, bad)


def test_frozen_parent_tag_checked_before_dockerfile(op, tmp_path):
    op.write_dockerfile(SimpleNamespace(run=lambda args: SimpleNamespace(stdout=op.BASE+'\n')), tmp_path)
    assert (tmp_path/'Dockerfile').read_text() == 'FROM '+op.TAG+'\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n'


def test_pending_runtime_pin_refuses_before_helper_or_docker(op, monkeypatch):
    op.FROZEN_RUNTIME_SHA256 = {wrapper.WHOLE: 'PENDING'}
    assert not op.qualification_constants_ready()
    def forbidden(*args, **kwargs): raise RuntimeError('Must not load helpers')
    monkeypatch.setattr(op, 'load', forbidden)
    import sys
    monkeypatch.setattr(sys, 'argv', ['qualify', '--archive', '/owned/input', '--sha256', 'a'*64,
                                    '--suite-helper', '/owned/suite', '--retention-helper', '/owned/retention'])
    with pytest.raises(AssertionError, match='STILL_PENDING'): op.main()
