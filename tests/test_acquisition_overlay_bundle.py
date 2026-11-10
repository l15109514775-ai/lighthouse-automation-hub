import hashlib
import io
import tarfile

import pytest

from scripts.test_acquisition_overlay_bundle import FILES, read_bundle


def bundle(tmp_path, mutation=None):
    archive = tmp_path / 'overlays.tar'
    values = {name: b'known-content' for name in FILES}
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
    manifest = {'version': 'acquisition-effective-release-v1', 'base_image': 'sha256:' + 'a' * 64,
        'overlay_archive_bytes': len(raw), 'overlay_archive_sha256': hashlib.sha256(raw).hexdigest(),
        'files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in values.items()}}
    return archive, manifest, values


def test_restores_only_exact_hash_bound_allowlisted_bytes(tmp_path):
    archive, manifest, values = bundle(tmp_path)
    assert read_bundle(manifest, archive) == values
    assert list(tmp_path.iterdir()) == [archive]  # validation does not extract files


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
