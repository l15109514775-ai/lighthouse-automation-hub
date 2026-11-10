"""Real V5 bytes/SQLite fixture and isolated Chromium; no live platform calls."""
import subprocess
from pathlib import Path
import pytest
from test_replay_part_native_v5 import test_standalone_part_uses_real_delivery_and_one_native_file as run_owned
from test_replay_delivery_v5 import delivery, fifo, tracks
from test_replay_parts_v5 import publisher
from test_replay_part_native_v5 import test_actual_python_qa_reservation_and_node_raw_evidence_complete_same_bound_part as run_integration


@pytest.mark.timeout(120)
@pytest.mark.parametrize('scenario', ['redirect', 'lost_receipt', 'fresh_context', 'collapsed_title', 'wrong_source', 'missing_cover',
    'changed_intent', 'existing_submit', 'challenge', 'duplicate_other_page', 'foreign_popup'])
def test_saved_upload_is_reconciled_without_another_selection_or_save(delivery, monkeypatch, scenario):
    original = subprocess.run
    fixture = Path(__file__).with_name('replay_draft_save_acknowledgement_fixture.mjs')
    def owned_run(arguments, **kwargs):
        if isinstance(arguments, list) and len(arguments)>1 and Path(arguments[1]).name=='replay_part_native_v5_fixture.mjs':
            arguments = [arguments[0], str(fixture), *arguments[2:], scenario]
        return original(arguments, **kwargs)
    monkeypatch.setattr(subprocess, 'run', owned_run)
    run_owned(delivery, 'upload_one')


def test_preparation_wait_does_not_extend_submission_authority():
    from services.hub.replay_part_publish_worker_v5 import PartReplaySession
    session = PartReplaySession.__new__(PartReplaySession)
    session.closed = False
    sent = []
    session._send = sent.append
    def receive(seconds):
        assert seconds == 900
        request = sent[-1]
        return {'request_id': request['request_id'], 'operation': 'prepare', 'ok': True, 'result': {'state': 'prepared'}}
    session._receive = receive
    assert session._operation('prepare') == {'state': 'prepared'}


@pytest.mark.timeout(120)
def test_saved_draft_readback_passes_real_sqlite_qa_and_single_submit(publisher, monkeypatch):
    original = subprocess.Popen
    fixture = Path(__file__).with_name('replay_draft_save_acknowledgement_fixture.mjs')
    def owned_start(arguments, **kwargs):
        if isinstance(arguments, list) and len(arguments)>1 and Path(arguments[1]).name=='replay_part_native_v5_fixture.mjs':
            arguments = [arguments[0], str(fixture), *arguments[2:], 'python_integration']
        return original(arguments, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', owned_start)
    run_integration(publisher)
