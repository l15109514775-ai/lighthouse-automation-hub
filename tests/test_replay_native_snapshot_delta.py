"""Real media/checker/SQLite; omit only redundant native-window MP4 reads.

No platform/account/network, fake machine verifier, expected-hash substitution,
or patched sha256_file is used. The audit hook only observes actual file opens.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from test_short_video_web import short_video, PRINCIPAL
from test_replay_video_web import replay
from test_replay_machine_checks import owned
from test_replay_worker import queue, upload_observation
from services.hub import replay_machine_checks as machine
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_video_web import STATE
from services.hub.youtube_download import STATE as DOWNLOAD_STATE


_reads = []


def _audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        for file, calls in _reads:
            if os.fsdecode(args[0]) == str(file):
                calls.append(args)


sys.addaudithook(_audit)


@contextmanager
def file_reads(file):
    """Observe Python's actual open/os.open events without replacing reads."""
    calls = []
    record = (file, calls)
    _reads.append(record)
    try:
        yield calls
    finally:
        _reads.remove(record)


def reference(file):
    raw = file.read_bytes()
    return {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def put_native(f, observation):
    observation = deepcopy(observation)
    folder = f['s'].root/'media'/f['job_id']/'replay-browser'/f['attempt']
    folder.mkdir(parents=True, exist_ok=True)
    screenshot = folder/'pre-submit.png'
    # Real decoded v4 PNG bytes, clearly an owned fixture, not a real webpage.
    screenshot.write_bytes(Path(f['derived']['output_file']).read_bytes())
    inner = {key: value for key, value in observation.items()
             if key not in ('pre_submit_screenshot', 'native_observation_file', 'private_draft_saved')}
    inner['version'] = 'replay-native-form-observation-v1'
    raw = folder/'native-preparation-observation.json'
    raw.write_text(encode(inner), 'utf-8')
    observation.update(pre_submit_screenshot=reference(screenshot), native_observation_file=reference(raw), private_draft_saved=True)
    return observation


@pytest.fixture
def native(owned):
    f = owned
    assert queue(f) == f['job_id']
    service = f['service'];attempt = f['original'].parent.name
    service.bind_browser_attempt(PRINCIPAL, f['job_id'], attempt_id=attempt)
    binding = f['db'].get(f['job_id'])['payload']
    service.start_upload(PRINCIPAL, f['job_id'], attempt_id=attempt, site_uid=binding['site_uid'])
    service.record_upload(PRINCIPAL, f['job_id'], upload_observation(binding, attempt))
    f['attempt'] = attempt
    provenance = json.loads(Path(f['derived']['provenance_file']).read_bytes())
    common = {'version': service.NATIVE_PREPARATION_VERSION, 'job_id': f['job_id'], 'attempt_id': attempt,
        'site_uid': binding['site_uid'], 'source_id': binding['source_id'], 'source_url': binding['source_url'],
        'artifact_sha256': binding['artifact_sha256'], 'file_bytes': binding['file_bytes'],
        'metadata_sha256': binding['metadata_sha256'], 'binding_sha256': machine.digest(binding),
        'cover_sha256': provenance['output']['sha256'],
        'cover_provenance_sha256': reference(Path(f['derived']['provenance_file']))['sha256'],
        'upload_reference_sha256': 'b'*64, 'title': binding['metadata']['title'], 'description': binding['metadata']['description'],
        'creation_statement': '内容为转载', 'source': binding['source_url'], 'category': binding['metadata']['category_label'],
        'tags': binding['metadata']['tags'], 'metadata_verified': True, 'cover_applied': True,
        'server_file_hash_verified': False, 'human_review': False, 'per_artifact_owner_acceptance': False,
        'observed_at': datetime.now(timezone.utc).isoformat()}
    f['observation'] = put_native(f, common)
    return f


def call(f, phase):
    service = f['service']
    if phase == 'record':
        return service.record_native_prepare(PRINCIPAL, f['job_id'], f['observation'])
    return service.reserve_submit(PRINCIPAL, f['job_id'], attempt_id=f['attempt'],
        site_uid=f['db'].get(f['job_id'])['payload']['site_uid'], upload_reference_sha256='b'*64)


def before_phase(f, phase):
    if phase == 'reserve':
        call(f, 'record')


def test_default_paths_still_hash_and_each_native_qa_really_reads_entire_current_file(native):
    f = native;service = f['service']
    with file_reads(f['file']) as reads:
        service._source(f['source'])
    assert len(reads) == 1
    with file_reads(f['file']) as reads:
        service.snapshot(f['job_id'])
    assert len(reads) == 1
    with file_reads(f['file']) as reads:
        call(f, 'record')
    assert len(reads) == 1  # Real read_checked; no preceding duplicate _source hash.
    with file_reads(f['file']) as reads:
        _, _, latest = service.snapshot(f['job_id'], write=False)
    assert reads == [] and latest['upload_observation']['upload_reference_sha256'] == 'b'*64
    with file_reads(f['file']) as reads:
        assert call(f, 'reserve')['can_click_submit'] is True
    assert len(reads) == 1
    proof = f['db'].get(f['job_id'])['result'][STATE]['machine_acceptance']
    assert proof['checks']['download']['artifact'] == reference(f['file'])
    assert proof['checks']['download']['packet_evidence'] == f['packets']
    assert proof['checks']['cover']['source_rgb_sha256'] == proof['checks']['cover']['foreground_rgb_sha256']
    receipts = list((f['s'].root/'media'/f['job_id']/'replay-machine-checks-v1').glob('*/receipt.json'))
    assert len(receipts) == 2
    assert proof['checks']['download']['packet_verification']['mode'] == 'reused_after_current_full_sha'
    assert len(f['db'].query('SELECT * FROM publications')) == 1
    with pytest.raises(HubError, match='AUTOMATION_AUTHORIZATION_REQUIRED'):
        call(f, 'reserve')
    assert len(f['db'].query('SELECT * FROM publications')) == 1


@pytest.mark.parametrize('phase', ['record', 'reserve'])
@pytest.mark.parametrize('change', ['source_state', 'source_kind', 'source_version', 'source_payload', 'source_authority',
    'receipt_bytes', 'metadata', 'target_uid', 'policy', 'file_size', 'source_scope'])
def test_shared_nonhash_gates_still_reject_before_any_native_checkpoint_or_reservation(native, phase, change):
    f = native;before_phase(f, phase);db = f['db'];job = db.get(f['job_id']);source = db.get(f['source'])
    if change.startswith('source_'):
        if change == 'source_state':
            with db.transaction() as con:con.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (f['source'],))
        elif change == 'source_kind':
            with db.transaction() as con:con.execute("UPDATE jobs SET job_type='owned_wrong_kind' WHERE job_id=?", (f['source'],))
        else:
            if change == 'source_version':source['result'][DOWNLOAD_STATE]['version'] = 'wrong-owned-version'
            elif change == 'source_payload':source['payload']['title'] = 'Changed immutable payload'
            elif change == 'source_authority':source['result']['publication_authorized'] = True
            else:source['payload']['raw']['status']['privacyStatus'] = 'private'
            with db.transaction() as con:con.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?', (encode(source['payload']), encode(source['result']), f['source']))
    elif change == 'receipt_bytes':
        file = f['file'].with_name('verified.json');file.write_bytes(file.read_bytes()+b' ')
    elif change in ('metadata', 'target_uid'):
        if change == 'metadata':job['payload']['metadata']['title'] = 'Changed title'
        else:job['payload']['site_uid'] = '999999'
        with db.transaction() as con:con.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(job['payload']), f['job_id']))
    elif change == 'policy':
        file = f['fixture'][5];file.write_bytes(file.read_bytes()+b'\n# changed owned policy bytes\n')
    else:
        with f['file'].open('ab') as stream:stream.write(b'changed-size')
    with file_reads(f['file']) as reads:
        with pytest.raises(HubError):call(f, phase)
    assert reads == []  # Cheap original predicates reject; no fake successful hash.
    assert not db.query('SELECT * FROM publications')


@pytest.mark.parametrize('phase', ['record', 'reserve'])
def test_same_size_artifact_change_is_rejected_by_actual_current_machine_hash(native, phase):
    f = native;before_phase(f, phase)
    raw = bytearray(f['file'].read_bytes());raw[-1] ^= 1;f['file'].write_bytes(raw)
    with file_reads(f['file']) as reads:
        with pytest.raises(HubError, match='ARTIFACT_HASH_MISMATCH'):call(f, phase)
    assert len(reads) == 1 and not f['db'].query('SELECT * FROM publications')
    with pytest.raises(HubError, match='VERIFIED_DOWNLOAD_REQUIRED'):f['service']._source(f['source'])


@pytest.mark.parametrize('phase', ['record', 'reserve'])
def test_source_race_after_shared_nonhash_check_is_rejected_by_real_checker(native, phase, monkeypatch):
    f = native;before_phase(f, phase);actual = f['service']._validate_snapshot_binding
    def change_after_check(*args, **kwargs):
        value = actual(*args, **kwargs)
        with f['db'].transaction() as con:con.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (f['source'],))
        return value
    monkeypatch.setattr(f['service'], '_validate_snapshot_binding', change_after_check)
    with pytest.raises(HubError, match='VERIFIED_SOURCE_REQUIRED'):call(f, phase)
    assert not f['db'].query('SELECT * FROM publications')


@pytest.mark.parametrize('phase', ['record', 'reserve'])
def test_dynamic_scope_still_checks_coherently_rebound_private_source(native, phase):
    f = native;before_phase(f, phase);db = f['db'];source = db.get(f['source'])
    source['payload']['raw']['status']['privacyStatus'] = 'private'
    cp = source['result'][DOWNLOAD_STATE];cp['binding']['source_payload_sha256'] = machine.digest(source['payload'])
    receipt = {'version': cp['version'], 'binding_sha256': machine.digest(cp['binding']), 'source_asset': cp['source_asset'],
        'receipt': cp['receipt'], 'human_review': False, 'publication_authorized': False, 'creator_original_bytes_verified': False}
    file = f['file'].with_name('verified.json');file.write_text(encode(receipt), 'utf-8')
    job = db.get(f['job_id']);job['payload']['download_receipt_sha256'] = reference(file)['sha256']
    with db.transaction() as con:
        con.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?', (encode(source['payload']), encode(source['result']), f['source']))
        con.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(job['payload']), f['job_id']))
    with file_reads(f['file']) as reads:
        with pytest.raises(HubError, match='SOURCE_SCOPE_REQUIRED'):call(f, phase)
    assert reads == [] and not db.query('SELECT * FROM publications')


@pytest.mark.parametrize('phase', ['record', 'reserve'])
def test_worker_lease_lost_during_actual_file_read_stops_checkpoint(native, phase, monkeypatch):
    f = native;before_phase(f, phase);db = f['db']
    with db.transaction() as con:con.execute("UPDATE jobs SET state='pending' WHERE job_id=?", (f['job_id'],))
    claimed = db.claim('video', 180, 4);assert claimed['job_id'] == f['job_id']
    actual = machine.read_checked
    def read_then_lose_lease(root, file, **kwargs):
        value = actual(root, file, **kwargs)
        if Path(file) == f['file']:
            with db.transaction() as con:con.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?', (f['job_id'],))
        return value
    monkeypatch.setattr(machine, 'read_checked', read_then_lose_lease)
    with f['service'].worker_context(claimed):
        with pytest.raises(HubError, match='LEASE_LOST'):call(f, phase)
    assert not db.query('SELECT * FROM publications')


@pytest.mark.parametrize('phase', ['record', 'reserve'])
def test_expired_native_observation_is_not_refreshed_by_real_machine_qa(native, phase):
    f = native;before_phase(f, phase)
    old = deepcopy(f['observation']);old['observed_at'] = (datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
    f['observation'] = put_native(f, old)
    if phase == 'reserve':
        job = f['db'].get(f['job_id']);job['result'][STATE]['native_preparation'] = f['observation']
        with f['db'].transaction() as con:con.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(job['result']), f['job_id']))
    with file_reads(f['file']) as reads:
        with pytest.raises(HubError, match='NATIVE_PREPARATION_REQUIRED'):call(f, phase)
    assert len(reads) == 1 and not f['db'].query('SELECT * FROM publications')
    assert f['observation']['observed_at'] == old['observed_at']


def test_actual_worker_handler_latest_upload_reference_snapshot_does_not_hash(owned, monkeypatch):
    from test_replay_video_worker_native import transport
    from services.hub.replay_video_browser import ReplayVideoWorkerHandler
    from services.hub.replay_video_web import JOB_TYPE
    from services.hub.worker import Worker
    f = owned;job_id = queue(f);service = f['service'];attempt = f['original'].parent.name
    service.bind_browser_attempt(PRINCIPAL, job_id, attempt_id=attempt)
    actual = service.snapshot;calls = []
    def observe_snapshot(*args, **kwargs):
        result = actual(*args, **kwargs)
        calls.append((result[0]['stage'], kwargs.get('write', True)))
        return result
    monkeypatch.setattr(service, 'snapshot', observe_snapshot)
    browser, counts = transport(f)
    handler = ReplayVideoWorkerHandler(f['s'], f['db'], service=service, browser=browser, cover_provider=lambda *args: None)
    assert Worker(f['s'], f['db'], {JOB_TYPE: handler.process}).once('video')
    # One snapshot obtains latest upload-reference; the next is reserve_submit.
    assert [write for stage, write in calls if stage == 'replay_native_prepared'] == [False, False]
    assert counts == {'upload': 1, 'prepare': 1, 'submit': 1, 'lookup': 0, 'closed': 1}
    assert f['db'].get(job_id)['state'] == 'awaiting_moderation'
