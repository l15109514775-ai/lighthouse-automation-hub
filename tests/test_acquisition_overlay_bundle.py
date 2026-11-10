import hashlib
import io
import json
import tarfile

import pytest

from scripts.test_acquisition_overlay_bundle import FILES, SOURCE_BINDINGS, SOURCE_BINDING_VERSIONS, read_bundle
from scripts.prepare_acquisition_source_bindings import prepare


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
