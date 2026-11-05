"""Owned unit-text fixtures only; no service, collector main, network or secrets."""
import importlib.util
from pathlib import Path
import pytest
OPS=Path(__file__).resolve().parents[2]/'scripts/ops'
spec=importlib.util.spec_from_file_location('proof_collector',OPS/'collect_cloudbase_v1e_safe_proof.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
@pytest.fixture(autouse=True)
def isolated_files(monkeypatch):
    monkeypatch.setattr(c,'FILES',{});monkeypatch.setattr(c,'TOTAL',0)

@pytest.mark.parametrize('name',list(c.UNIT_SHA256))
def test_only_exact_public_unit_template_is_copied(name):
    path=OPS/name;c.unit_file(name,path)
    assert c.FILES['units/'+name]['data']==path.read_bytes()
    assert c.digest(c.FILES['units/'+name]['data'])==c.UNIT_SHA256[name]

@pytest.mark.parametrize('added',[b'\nEnvironment=API_KEY=owned-ordinary-value\n',b'\nPassword=owned-ordinary-value\n'])
def test_plain_sensitive_assignment_not_caught_by_literal_regex_still_cannot_enter_archive(tmp_path,added):
    name='lighthouse-cloudbase-backup.service';data=(OPS/name).read_bytes()+added
    assert not c.LITERAL.search(data) # Demonstrates why generic pattern checks alone were insufficient.
    path=tmp_path/name;path.write_bytes(data)
    with pytest.raises(AssertionError):c.unit_file(name,path)
    assert not c.FILES


def test_unknown_unit_is_rejected_before_any_file_is_opened(tmp_path):
    with pytest.raises(AssertionError):c.unit_file('unrelated.service',tmp_path/'does-not-exist')
    assert not c.FILES
