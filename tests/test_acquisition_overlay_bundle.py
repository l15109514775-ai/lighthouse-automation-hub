import hashlib
import io
import json
import tarfile

import pytest

from scripts.test_acquisition_overlay_bundle import FILES, SOURCE_BINDINGS, SOURCE_BINDING_VERSIONS, read_bundle
from scripts.prepare_acquisition_source_bindings import prepare, stage_installed_override


def bundle(tmp_path, mutation=None, *, files=FILES, version='acquisition-effective-release-v1'):
    archive = tmp_path / 'overlays.tar'
    values = {name: b'known-content' for name in files}
    with tarfile.open(archive, 'w') as tar:
        for index, name in enumerate(sorted(values)):
            member = tarfile.TarInfo(name)
            data = values[name]
            member.size = len(data)
            if mutation == 'traversal' and index == 0: member.name = '../outside.py'
            if mutation == 'symlink' and index == 0:
                member.type = tarfile.SYMTYPE; member.linkname = '/etc/passwd'; member.size = 0
            tar.addfile(member, io.BytesIO(data) if member.isfile() else None)
            if mutation == 'duplicate' and index == 0: tar.addfile(member, io.BytesIO(data))
    raw = archive.read_bytes()
    manifest = {'version': version, 'base_image': 'sha256:' + 'a' * 64,
        'overlay_archive_bytes': len(raw), 'overlay_archive_sha256': hashlib.sha256(raw).hexdigest(),
        'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in values.items()}}
    return archive, manifest, values


def test_restores_only_exact_hash_bound_allowlisted_bytes(tmp_path):
    archive, manifest, values = bundle(tmp_path)
    assert read_bundle(manifest, archive) == values
    assert list(tmp_path.iterdir()) == [archive]  # validation does not extract files


def test_source_bindings_have_their_own_exact_allowlist(tmp_path):
    archive, manifest, values = bundle(tmp_path, files=SOURCE_BINDINGS,
                                       version='acquisition-source-bindings-v1')
    assert read_bundle(manifest, archive) == values
    manifest['version'] = 'acquisition-effective-release-v1'
    with pytest.raises(ValueError, match='OVERLAY_MANIFEST_INVALID'):
        read_bundle(manifest, archive)


@pytest.mark.parametrize('version', sorted(SOURCE_BINDING_VERSIONS))
def test_prepared_override_requires_new_directory_and_only_mounts_reviewed_code(tmp_path, version):
    archive, manifest, files = bundle(tmp_path, files=SOURCE_BINDING_VERSIONS[version], version=version)
    out = tmp_path / 'release'
    declaration = json.loads(prepare(manifest, archive, out).read_bytes())
    assert set(declaration['services']) == {'replay-acquisition-worker'}
    service = declaration['services']['replay-acquisition-worker']
    assert service['image'] == manifest['base_image'] and service['pull_policy'] == 'never'
    mounts = {m['target']: m for m in service['volumes']}
    assert set(mounts) == {'/opt/lighthouse-hub/' + k for k in files}
    for name, content in files.items():
        m = mounts['/opt/lighthouse-hub/' + name]
        assert m['source'] == str(out / name) and m['read_only'] and not m['bind']['create_host_path']
        assert (out / name).read_bytes() == content
    with pytest.raises(FileExistsError):
        prepare(manifest, archive, out)


def test_new_release_requires_both_dispatcher_and_source_guard(tmp_path):
    archive, manifest, _ = bundle(tmp_path, files=SOURCE_BINDINGS, version='acquisition-source-bindings-v2')
    with pytest.raises(ValueError, match='OVERLAY_MANIFEST_INVALID'):
        read_bundle(manifest, archive)


def test_installed_override_preserves_private_settings_and_exact_previous_bytes(tmp_path):
    version = 'acquisition-source-bindings-v2'
    archive, manifest, files = bundle(tmp_path, files=SOURCE_BINDING_VERSIONS[version], version=version)
    candidate = prepare(manifest, archive, tmp_path / 'release')
    role = 'replay-acquisition-worker'
    program = ['python', '-m', 'services.hub.replay_multipart_worker_runtime_v4', '--acquisition-only']
    data = {'type': 'bind', 'source': '/private/data', 'target': '/opt/lighthouse-hub/data'}
    code = {'type': 'bind', 'source': '/previous/worker.py', 'read_only': True,
            'target': '/opt/lighthouse-hub/services/hub/worker.py'}
    installed = {'services': {role: {'image': 'previous', 'command': program, 'volumes': [data, code],
                                     'secrets': ['youtube']}}, 'secrets': {'youtube': {'file': '/private/key'}}}
    previous = tmp_path / 'installed.json'
    original = (json.dumps(installed) + '\r\n').encode()
    previous.write_bytes(original)
    replacement = stage_installed_override(previous, candidate, 'previous')
    assert (candidate.parent / 'installed-previous.compose.json').read_bytes() == previous.read_bytes() == original
    result = json.loads(replacement.read_bytes())
    service = result['services'][role]
    assert result['secrets'] == installed['secrets'] and service['secrets'] == ['youtube']
    assert service['command'] == program and service['image'] == manifest['base_image']
    assert service['volumes'][0] == data and len(service['volumes']) == len(files) + 1
    assert service['volumes'][1]['source'] == str(candidate.parent / 'services/hub/worker.py')
    with pytest.raises(ValueError, match='INSTALLED_ACQUISITION_RELEASE_MISMATCH'):
        stage_installed_override(previous, candidate, 'other')


@pytest.mark.parametrize('mutation', ['traversal', 'symlink', 'duplicate'])
def test_rejects_malicious_members_even_with_matching_archive_hash(tmp_path, mutation):
    archive, manifest, _ = bundle(tmp_path, mutation)
    with pytest.raises(ValueError, match='OVERLAY_MEMBER'):
        read_bundle(manifest, archive)


def test_rejects_archive_changed_since_manifest(tmp_path):
    archive, manifest, _ = bundle(tmp_path)
    raw = bytearray(archive.read_bytes()); raw[-1] = 1; archive.write_bytes(raw)
    with pytest.raises(ValueError, match='OVERLAY_ARCHIVE_CHANGED'):
        read_bundle(manifest, archive)


def test_rejects_member_digest_mismatch(tmp_path):
    archive, manifest, _ = bundle(tmp_path)
    manifest['files']['services/hub/worker.py']['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='OVERLAY_MEMBER_CHANGED'):
        read_bundle(manifest, archive)
