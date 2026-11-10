"""Real local Git objects; deterministic offline export without history/secrets."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import pytest

spec = importlib.util.spec_from_file_location('locked_export', Path(__file__).parents[1] / 'scripts/export_locked_source.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), '-c', 'user.name=Owned Fixture',
                                   '-c', 'user.email=fixture@example.invalid', '-c', 'core.autocrlf=false',
                                   *args], stderr=subprocess.PIPE)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    git(root, 'init', '-q')
    lock = {}
    for folder in ('compositor', 'vendor/subtitle-cloud-worker'):
        sub = root / folder
        sub.mkdir(parents=True)
        git(sub, 'init', '-q')
        (sub / 'owned.txt').write_bytes(b'owned\nfixture\n')
        git(sub, 'add', 'owned.txt')
        git(sub, 'commit', '-qm', 'owned fixture')
        commit = git(sub, 'rev-parse', 'HEAD').decode().strip()
        lock[folder] = {'commit': commit, 'files': {'owned.txt': hashlib.sha256(b'owned\r\nfixture\r\n').hexdigest()}}
        git(root, 'update-index', '--add', '--cacheinfo', '160000,' + commit + ',' + folder)
    (root / 'config').mkdir()
    (root / 'config/upstream-lock.json').write_text(json.dumps(lock))
    (root / 'README.md').write_bytes(b'Owned export fixture\n')
    git(root, 'add', 'config/upstream-lock.json', 'README.md')
    git(root, 'commit', '-qm', 'owned hub')
    return root


def test_deterministic_export_contains_locked_bytes_not_dirty_or_private_files(source, tmp_path):
    (source / 'README.md').write_text('uncommitted edit')
    (source / '.env').write_text('SYNTHETIC_SECRET=do-not-export')
    (source / 'compositor/owned.txt').write_text('dirty upstream edit')
    a, b = tmp_path / 'a.tar.gz', tmp_path / 'b.tar.gz'
    first = module.export(source, a)
    second = module.export(source, b)
    assert a.read_bytes() == b.read_bytes() and first == second
    with tarfile.open(a) as archive:
        assert '.env' not in archive.getnames()
        assert not any('/.git/' in n for n in archive.getnames())
        assert archive.extractfile('README.md').read() == b'Owned export fixture\n'
        assert archive.extractfile('compositor/owned.txt').read() == b'owned\r\nfixture\r\n'
        assert json.load(archive.extractfile('BUILD_SOURCE_MANIFEST.json'))['image_qualified'] is False


def test_existing_export_is_never_overwritten(source, tmp_path):
    target = tmp_path / 'prior.tar.gz'
    target.write_bytes(b'previous release')
    with pytest.raises(ValueError, match='EXISTS'):
        module.export(source, target)
    assert target.read_bytes() == b'previous release'


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'data/hub.sqlite3', 'secrets/key',
                                   '.env', 'config/hub.toml', 'nested/.git/config', 'C:/escape'])
def test_private_and_unsafe_archive_paths_are_rejected(name):
    with pytest.raises(ValueError):
        module.checked_name(name)


def test_wrong_lock_cannot_be_materialized_or_written(source, tmp_path):
    path = source / 'config/upstream-lock.json'
    lock = json.loads(path.read_text())
    lock['compositor']['files']['owned.txt'] = '0' * 64
    path.write_text(json.dumps(lock))
    git(source, 'add', 'config/upstream-lock.json')
    git(source, 'commit', '-qm', 'owned wrong lock')
    target = tmp_path / 'bad.tar.gz'
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        module.export(source, target)
    assert not target.exists()


def test_tracked_live_configuration_refuses_export(source, tmp_path):
    (source / 'config/hub.toml').write_text('synthetic_private = true')
    git(source, 'add', 'config/hub.toml')
    git(source, 'commit', '-qm', 'owned private config')
    target = tmp_path / 'private.tar.gz'
    with pytest.raises(ValueError, match='LIVE_CONFIG'):
        module.export(source, target)
    assert not target.exists()
