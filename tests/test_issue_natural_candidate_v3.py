"""Pure candidate preparation. No real pilot, root file or account access."""
from pathlib import Path
import json,tomllib
import pytest
from scripts.ops import issue_natural_candidate_v3 as c


def test_only_top_level_activation_changes_and_original_permissions_text_is_preserved():
    raw=b'\xef\xbb\xbf# Owned\r\nactivated_at = "UNCONFIGURED" # actual time only\r\nenabled = true\r\nowner_permission_reference = "Owned unchanged"\r\n[nested]\r\nactivated_at = "not changed"\r\n'
    instant='2026-11-01T20:00:00+00:00';value=c.activate_at(raw,instant)
    expected=tomllib.loads(raw.decode('utf-8-sig'));expected['activated_at']=instant
    assert tomllib.loads(value.decode('utf-8-sig'))==expected
    assert value.startswith(b'\xef\xbb\xbf# Owned\r\n') and b'# actual time only\r\n' in value
    assert b'[nested]\r\nactivated_at = "not changed"\r\n' in value


@pytest.mark.parametrize('raw',[b'enabled=true\n',b'activated_at = 0\n',b'[nested]\nactivated_at="Owned"\n'])
def test_missing_or_unrecognized_activation_is_not_invented(raw):
    with pytest.raises(AssertionError):c.activate_at(raw,'2026-11-01T20:00:00+00:00')


@pytest.mark.parametrize('extra',[None,'missing','empty','credential'])
def test_only_two_actual_workflow_authorization_fields_are_accepted(extra):
    value={'actual_owner_workflow_response':'Owned fixture only, not real authorization','owner_response_reference':'Owned fixture'}
    if extra=='missing':value.pop('owner_response_reference')
    elif extra=='empty':value['actual_owner_workflow_response']=' '
    elif extra=='credential':value['API_KEY']='Owned placeholder must not be accepted'
    if extra is None:assert c.authorization(json.dumps(value))==value
    else:
        with pytest.raises(AssertionError):c.authorization(json.dumps(value))


def test_installer_capture_is_executed_without_a_second_file_read(tmp_path,monkeypatch):
    file=tmp_path/'owned-installer.py';raw=b'VALUE=7\n';file.write_bytes(raw)
    monkeypatch.setattr(c,'INSTALLER_SHA',c.hashlib.sha256(raw).hexdigest());original=Path.read_bytes
    def swap(path):
        result=original(path)
        if path==file:file.write_bytes(b'VALUE=999\n')
        return result
    monkeypatch.setattr(Path,'read_bytes',swap)
    assert c.load(file).VALUE==7


def test_issuer_is_copy_only_offline_and_gates_all_consumption_before_genuine_pilot_call():
    compile(c.ISSUE,'Owned issuer','exec');point=c.ISSUE.index('value=issue_launch(')
    assert c.ISSUE.index('socket.socket.connect=refuse')<point
    assert c.ISSUE.index('Worker.once=Worker.run=QueueView.claim=QueueView.recover=threading.Thread.start=refuse')<point
    assert "Store('/run/issuer-db/hub.sqlite3')" in c.ISSUE
    assert "'594aed06-3aa1-45f4-90f0-1db49a602395'" in c.ISSUE
    assert 'actual_owner_launch_response=auth' in c.ISSUE
    assert 'db.get(pilot)==prior' in c.ISSUE
    assert 'per_artifact_owner_acceptance' not in c.ISSUE.split('refs={}')[0] # No fabricated acceptance field.
