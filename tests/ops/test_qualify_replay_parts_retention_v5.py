"""Owned qualification/install protocol. No Docker, account or real records.

The synthetic parent manifest is an explicit cryptographic-input seam. These
checks do not claim that 276/26 production qualification tests have run here.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FILE = Path(__file__).resolve().parents[2]/'scripts/ops/qualify_replay_parts_retention_v5.py'
SPEC = importlib.util.spec_from_file_location('owned_retention_qualification', FILE)
m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)


def xml(count, group=None, bad=None):
    rows = []
    for index in range(count):
        name = group or (m.GROUP if index < 26 else m.CAPTURE_GROUP if index < 29 else m.GUI_GROUP if index == 29 else 'tests.owned_parent_protocol')
        inner = '<failure message="owned failure"/>' if bad == index else ''
        rows.append(f'<testcase classname="{name}" name="owned_{index}">{inner}</testcase>')
    return ('<testsuites><testsuite>' + ''.join(rows) + '</testsuite></testsuites>').encode()


@pytest.fixture
def qualified(monkeypatch):
    names = ['services/hub/replay_part_owner_handoff_v5.py', 'services/hub/replay_parts_worker_runtime_v5.py']
    names += [f'services/hub/owned_{i:03}.py' for i in range(128)]
    entries = {n: {'bytes': 1, 'sha256': 'a'*64} for n in names + list(m.NODE_SOURCES)}
    entries[m.HANDOFF_SOURCE]['sha256'] = m.HANDOFF_SHA
    manifest = {'version': 'tracked-source-candidate-v1', 'source_commit': m.PARENT_COMMIT,
        'files': entries, 'test_selections': [m.SELECTION]}
    monkeypatch.setattr(m, 'PARENT_MANIFEST', m.sha(m.wire(manifest)))
    junit = xml(276)
    value = {'version': 'tracked-source-candidate-ubuntu-result-v1', 'archive_sha256': m.PARENT_ARCHIVE,
        'source_commit': m.PARENT_COMMIT, 'source_manifest': manifest, 'image': 'sha256:'+'9'*64,
        'derived_gui_runtime_base_image': m.GUI_BASE, 'junit_sha256': m.sha(junit), 'tests': 276,
        'exit_code': 0, 'failure': 0, 'error': 0, 'skipped': 0, 'passed': True, 'network': 'none',
        **{k: False for k in ('production_data_mounted', 'secrets_injected', 'timed_out', 'oom_killed')},
        **{k: True for k in ('no_pid_or_oom_denial', 'mounted_sources_unchanged', 'runtime_matches_declared_candidate', 'empty_media_tmpfs')}}
    return value, junit


def authorization():
    return {'version': 'video-source-retention-v2', 'authorization_id': m.AUTHORIZATION,
        'scope': 'confirmed_full_duration_public_video', 'target_site_uid': m.UID, 'artifact': None,
        'formal_workflow_launch_claimed': False, 'publication_authorized': False, 'remote_video_deletion_authorized': False,
        'supersedes_legacy_local_test_hold': True, 'owner_response_reference': 'owned user instruction fixture',
        'actual_owner_response': '录播视频上传B站后不需要保留', 'recorded_at': '2026-11-02T00:00:00Z'}


def qualification(parent):
    raw = xml(26, m.GROUP)
    value = {'version': m.VERSION, 'tests': 26, 'test_groups': {m.GROUP: 26}, 'image': parent['image'],
        'source_files': m.source_files(parent['source_manifest']), 'source_manifest_sha256': m.PARENT_MANIFEST,
        'qualified_parent_sha256': m.sha(m.wire(parent)), 'source_archive_sha256': m.PARENT_ARCHIVE, 'source_commit': m.PARENT_COMMIT,
        'junit_file': str(m.CONTAINER_PROOF/'results.xml'), 'junit_sha256': m.sha(raw),
        'exit_code': 0, 'failures': 0, 'errors': 0, 'skipped': 0, 'network': 'none',
        **{k: True for k in ('full_suite_passed', 'no_pid_or_oom_denial', 'runtime_matches_declared_candidate', 'mounts_match_isolated_allowlist', 'mounted_sources_unchanged')},
        **{k: False for k in ('application_data_mounted', 'production_profile_mounted', 'secrets_environment_injected', 'oom_killed', 'timed_out')}}
    return value, raw


def test_parent_uses_real_complete_junit_not_passed_flag_and_preserves_frozen_manifest(qualified):
    parent, junit = qualified
    assert m.validate_parent(parent, junit) is parent
    failed = xml(276, bad=275); changed = {**parent, 'junit_sha256': m.sha(failed)}
    with pytest.raises(ValueError, match='ACTUAL_COMPLETE_JUNIT'):m.validate_parent(changed, failed)
    changed = deepcopy(parent); changed['source_manifest']['files'][m.NODE_SOURCES[0]]['sha256'] = 'b'*64
    with pytest.raises(ValueError, match='PARENT_MANIFEST_CHANGED'):m.validate_parent(changed, junit)
    # Old 273, GUI1-only or an inflated count cannot qualify this new source.
    for count in (1,273,275,277):
        with pytest.raises(ValueError,match='276_NOT_PASSED'):m.validate_parent({**parent,'tests':count},junit)
    old={**parent,'source_commit':'46a89ea1dd70d4d71cc099deae321f8fbe3b6b05','archive_sha256':'de13d7c41d310b8193a0b361aecff24aad18924cec2408e629be435bebdb7d78'}
    with pytest.raises(ValueError,match='WRONG_276_PARENT'):m.validate_parent(old,junit)
    missing_gui=junit.replace(m.GUI_GROUP.encode(),b'tests.owned_missing_gui')
    with pytest.raises(ValueError,match='PARENT_CAPTURE_AND_GUI'):m.validate_parent({**parent,'junit_sha256':m.sha(missing_gui)},missing_gui)


def test_qualified_source_scope_includes_handoff_runtime_and_excludes_tests_and_configuration(qualified):
    parent, _ = qualified; manifest = parent['source_manifest']
    files = m.source_files(manifest)
    assert len(files) == 141 and 'services/hub/replay_part_owner_handoff_v5.py' in files
    assert 'services/hub/replay_parts_worker_runtime_v5.py' in files
    assert not any(n.startswith(('tests/', 'config/')) for n in files)
    changed = deepcopy(manifest); changed['files'].pop(m.NODE_SOURCES[0])
    with pytest.raises(ValueError, match='EXACT_SOURCE_SCOPE'):m.source_files(changed)
    assert files[m.HANDOFF_SOURCE] == m.HANDOFF_SHA
    changed=deepcopy(manifest);changed['files'][m.HANDOFF_SOURCE]['sha256']='b04c70955e9ab1b2f6dd050da29574293bfee34f7a0823f8d1e75c7f1ddda30c'
    with pytest.raises(ValueError,match='QUALIFIED_HANDOFF_SOURCE'):m.source_files(changed)


def test_owner_template_binds_existing_full_record_and_never_grants_publication_or_remote_deletion(qualified):
    parent, _ = qualified; proof, _ = qualification(parent); original = authorization()
    raw = m.wire(proof); release = m.release_template(original, proof, raw)
    assert release['authorization_id'] == m.AUTHORIZATION
    assert release['authorization_sha256'] == m.sha(m.wire(original))
    assert release['test_attestation'] == {'file': str(m.CONTAINER_PROOF/'test-attestation.json'), 'sha256': m.sha(raw)}
    for key, value in [('authorization_id', '00000000-0000-0000-0000-000000000000'), ('formal_workflow_launch_claimed', True), ('artifact', {'file': 'owned-other-scope'})]:
        with pytest.raises(ValueError):m.owner({**original, key: value})


def isolated(image, owned, seccomp):
    profile = {'defaultAction': 'SCMP_ACT_ERRNO', 'syscalls': []}
    info = {'Image': image, 'Config': {'User': '10001:10001', 'Env': ['PATH=/owned/bin', 'HOME=/tmp/home']},
        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Init': True, 'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges:true', 'seccomp='+json.dumps(profile)], 'Memory': 1400*1024**2,
            'NanoCpus': 1_000_000_000, 'PidsLimit': 256, 'ShmSize': 256*1024**2,
            'Tmpfs': {'/tmp': 'rw,size=256m,mode=1777', str(m.ROOT/'media'): 'rw,size=32m,mode=0700,uid=10001,gid=10001'}},
        'Mounts': [{'Type': 'bind', 'Source': str(owned), 'Destination': '/owned', 'RW': True}]}
    return info, profile


def test_runtime_handles_docker_expanded_seccomp_and_rejects_production_mount_network_and_secrets(qualified):
    parent, _ = qualified; owned = Path('/owned-host'); seccomp = Path('/protected/browser-seccomp.json')
    info, profile = isolated(parent['image'], owned, seccomp)
    check = lambda v: m.validate_isolation(v, image=parent['image'], owned=owned, seccomp=seccomp, seccomp_profile=profile)
    assert check(info)
    for change in ('network', 'mount', 'memory', 'seccomp', 'secret'):
        wrong = deepcopy(info)
        if change == 'network':wrong['HostConfig']['NetworkMode'] = 'bridge'
        elif change == 'mount':wrong['Mounts'].append({'Type': 'bind', 'Source': '/actual/data', 'Destination': '/opt/lighthouse-hub/data', 'RW': True})
        elif change == 'memory':wrong['HostConfig']['Memory'] += 1
        elif change == 'seccomp':wrong['HostConfig']['SecurityOpt'][1] = 'seccomp=unconfined'
        else:wrong['Config']['Env'].append('YOUTUBE_API_KEY=owned-test-value')
        with pytest.raises((ValueError, json.JSONDecodeError)):check(wrong)


def test_standalone_26_qualification_rejects_filtered_xml_wrong_image_and_bad_resource_evidence(qualified):
    parent, _ = qualified; proof, xmlraw = qualification(parent)
    check = lambda value, raw:m.validate_qualification(value, raw, parent, parent_sha256=m.sha(m.wire(parent)))
    assert check(proof, xmlraw) is proof
    for key, value in [('image', 'sha256:'+'4'*64), ('mounted_sources_unchanged', False), ('no_pid_or_oom_denial', False), ('application_data_mounted', True)]:
        with pytest.raises(ValueError):check({**proof, key: value}, xmlraw)
    with pytest.raises(ValueError, match='QUALIFICATION_PARENT_CHANGED'):check({**proof, 'qualified_parent_sha256': 'f'*64}, xmlraw)
    truncated = xml(25, m.GROUP)
    with pytest.raises(ValueError):check({**proof, 'junit_sha256': m.sha(truncated)}, truncated)


def test_install_rejects_wrong_running_image_before_any_container_exec_or_file_write(qualified, monkeypatch):
    parent, _ = qualified; proof, _ = qualification(parent); calls = []
    cid = 'c'*64
    wrong = {'Id': cid, 'State': {'Running': True}, 'Image': 'sha256:'+'4'*64}
    def run(argv, **kw):calls.append(argv); return SimpleNamespace(stdout=json.dumps([wrong]))
    monkeypatch.setattr(m, 'run', run)
    monkeypatch.setattr(m, 'save', lambda *a, **k:pytest.fail('No write before verified running image'))
    with pytest.raises(ValueError, match='PUBLISHER_RUNTIME_CHANGED'):m.apply_release(parent, proof, m.wire(proof), authorization(), cid)
    assert calls == [['docker', 'inspect', cid]]


def test_install_writes_only_release_after_exact_source_exec_and_readonly_proof_mount(qualified, monkeypatch):
    parent, _ = qualified; proof, _ = qualification(parent); cid = 'c'*64; files = {}; calls = []
    proof['host_file'] = str(m.BASE/'00000000-0000-0000-0000-000000000001'/'test-attestation.json')
    info = {'Id': cid, 'State': {'Running': True}, 'Image': parent['image'], 'Config': {'User': '10001:10001'},
        'HostConfig': {'ReadonlyRootfs': True}, 'Mounts': [{'Type': 'bind', 'Source': str(Path(proof['host_file']).parent), 'Destination': str(m.CONTAINER_PROOF), 'RW': False}]}
    monkeypatch.setattr(m, 'run', lambda argv, **kw:SimpleNamespace(stdout=json.dumps([info])))
    def execute(argv, **kw):
        calls.append((argv, kw)); assert json.loads(kw['input']) == proof['source_files']
        return SimpleNamespace(stdout='source_sha256_verified\n')
    monkeypatch.setattr(m.subprocess, 'run', execute)
    monkeypatch.setattr(m, 'save', lambda file, raw, **kw:files.__setitem__(str(file), raw))
    monkeypatch.setattr(m, 'read', lambda file, **kw:files[str(file)])
    result = m.apply_release(parent, proof, m.wire(proof), authorization(), cid)
    assert result['state'] == 'release_installed' and result['services_restarted'] == result['source_files_deployed'] == result['media_deleted'] == 0
    assert set(files) == {str(m.ROOT/'config'/m.RELEASE)}
    assert len(calls) == 1 and calls[0][0][:6] == ['docker', 'exec', '--user', '10001:10001', '-i', cid]
    assert not any(word in calls[0][0] for word in ('stop', 'restart', 'create', 'up'))
