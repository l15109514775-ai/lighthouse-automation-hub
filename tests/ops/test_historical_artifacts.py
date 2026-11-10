import hashlib
import pytest
import historical_artifacts as module


def test_missing_original_is_not_replaced_by_a_generated_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('LIGHTHOUSE_TEST_ARTIFACT_DIR', str(tmp_path))
    with pytest.raises(FileNotFoundError, match='HISTORICAL_ARTIFACT_MISSING'):
        module.historical_artifact(next(iter(module.PINS)))
    assert list(tmp_path.iterdir()) == []


def test_owned_hash_fixture_rejects_corruption(tmp_path, monkeypatch):
    name = next(iter(module.PINS))
    data = b'Owned test input; not a historical qualification archive'
    monkeypatch.setitem(module.PINS, name, hashlib.sha256(data).hexdigest())
    monkeypatch.setenv('LIGHTHOUSE_TEST_ARTIFACT_DIR', str(tmp_path))
    path = tmp_path / name
    path.write_bytes(data)
    assert module.historical_artifact(name) == path
    path.write_bytes(data + b'changed')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        module.historical_artifact(name)


def test_unregistered_path_is_rejected_before_filesystem_access():
    with pytest.raises(ValueError, match='NOT_REGISTERED'):
        module.historical_artifact('../config/private.json')


def test_member_reader_requires_exact_hash_and_never_extracts(tmp_path, monkeypatch):
    import io, tarfile
    name=next(iter(module.PINS));file=tmp_path/name;raw=b'Owned historical member fixture'
    with tarfile.open(file,'w') as archive:
        item=tarfile.TarInfo('scripts/owned.py');item.size=len(raw)
        archive.addfile(item,io.BytesIO(raw))
    monkeypatch.setenv('LIGHTHOUSE_TEST_ARTIFACT_DIR',str(tmp_path))
    monkeypatch.setitem(module.PINS,name,hashlib.sha256(file.read_bytes()).hexdigest())
    assert module.historical_member(name,'scripts/owned.py',hashlib.sha256(raw).hexdigest())==raw
    with pytest.raises(ValueError,match='HASH_MISMATCH'):
        module.historical_member(name,'scripts/owned.py','0'*64)
    assert not (tmp_path/'scripts').exists()


@pytest.mark.parametrize('member',['../private','/absolute','scripts/missing.py'])
def test_member_reader_rejects_unsafe_or_absent_names(tmp_path,monkeypatch,member):
    import tarfile
    name=next(iter(module.PINS));file=tmp_path/name
    with tarfile.open(file,'w'):pass
    monkeypatch.setenv('LIGHTHOUSE_TEST_ARTIFACT_DIR',str(tmp_path))
    monkeypatch.setitem(module.PINS,name,hashlib.sha256(file.read_bytes()).hexdigest())
    with pytest.raises(ValueError,match='MEMBER_INVALID'):
        module.historical_member(name,member,'0'*64)
