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
