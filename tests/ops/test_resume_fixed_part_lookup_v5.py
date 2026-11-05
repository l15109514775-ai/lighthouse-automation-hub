"""Owned SQLite transition tests; context is synthetic, not platform evidence."""
from copy import deepcopy
from pathlib import Path
import hashlib, importlib.util, json, time
import pytest
from services.hub.db import encode
import services.hub.replay_part_moderation_v5 as mod

path = Path(__file__).resolve().parents[2] / 'scripts/ops/resume_fixed_part_lookup_v5.py'
spec = importlib.util.spec_from_file_location('owned_fixed_lookup_resume', path)
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)


@pytest.fixture
def case(runtime, monkeypatch):
    settings, store = runtime
    mod.initialize(store)
    expected = {}
    for name in op.FILES:
        file = settings.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'owned source hash boundary only')
        expected[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    binding = {'parent_job_id': op.GROUP, 'site_uid': '1000000000000000', 'part_index': 0,
               'metadata_sha256': 'a' * 64, 'source_id': op.SOURCE,
               'source_url': 'https://www.youtube.com/watch?v=' + op.SOURCE}
    state = {'attempt_id': 'e3a44d84-5e8f-4b70-8b3b-8d63581b17c3', 'key': 'owned-key'}
    intent = {'sha256': op.INTENT_SHA, 'file': 'owned-only', 'bytes': 1}
    now = time.time()
    with store.transaction() as db:
        for ident, platform, kind, status, attempts, payload in (
            (op.GROUP, 'youtube', 'replay_parts_publish_parent_v5', 'upload_pending', 0, {}),
            (op.PUBLISH, 'youtube', 'replay_part_publish_v5_p01', 'publish_unknown', 2, binding),
            (op.LOOKUP, 'bilibili', mod.LOOKUP_JOB_TYPE, 'awaiting_review', 1, {}),
        ):
            db.execute('INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,error_code,policy_version,payload,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, platform, op.PUBLISH + ':lookup:1' if ident == op.LOOKUP else op.SOURCE, kind, 'video', status, now, now, attempts,
                        op.ERROR if ident == op.LOOKUP else None, mod.POLICY, encode(payload), encode({'keep': ident})))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   ('owned-key', op.PUBLISH, 'bilibili', 'youtube', op.SOURCE,
                    'site_uid:1000000000000000', 'video_website_replay_part_v5_p01', 'unknown', None, None, now))
        db.execute('UPDATE pipelines SET paused=1 WHERE name=\'video\'')
    def context(*_args):
        return store.get(op.PUBLISH), binding, state, store.query('SELECT * FROM publications')[0], intent, now
    monkeypatch.setattr(mod, 'parent_context', context)
    payload = mod.child_payload(context(), 1)
    with store.transaction() as db:
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode(payload), op.LOOKUP))
        db.execute('INSERT INTO replay_part_lookup_schedule_v5 VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (op.PUBLISH, op.digest(binding), op.INTENT_SHA, now, now + 3600, now, 1,
                    op.LOOKUP, 'blocked', encode({'last_code': op.ERROR})))
        db.execute('INSERT INTO replay_part_lookup_attempts_v5 VALUES(?,?,?,?,?,?,?)',
                   ('owned-ledger', op.LOOKUP, 'owned-old-lease', op.PUBLISH, now, 'reserved', None))
    refresh = lambda: op.snapshot(settings, store, expected)
    plan = {'version': op.VERSION, 'lookup_job_id': op.LOOKUP, 'publish_job_id': op.PUBLISH, 'snapshot': refresh()}
    return settings, store, expected, plan, refresh


def test_resume_only_lookup_preserves_original_attempt_and_publication(case):
    _, store, _, plan, refresh = case
    before = deepcopy(plan['snapshot'])
    result = op.apply(store, plan, refresh)
    child = store.get(op.LOOKUP)
    assert child['state'] == 'retry_scheduled' and child['attempts'] == 1
    assert child['lease_owner'] is child['lease_until'] is None
    assert child['payload'] == before['child']['payload'] and child['result'] == before['child']['result']
    assert op.digest(store.get(op.PUBLISH)) == before['parent_digest']
    assert op.digest(store.get(op.GROUP)) == before['group_digest']
    assert store.query('SELECT * FROM publications') == [before['publication']]
    assert store.query('SELECT * FROM replay_part_lookup_attempts_v5') == before['ledger']
    schedule = store.query('SELECT * FROM replay_part_lookup_schedule_v5')[0]
    assert {**schedule, 'status': 'blocked'} == before['schedule']
    assert result['upload_or_submit_requests'] == result['provider_requests'] == 0
    audits = store.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))
    assert len(audits) == 1 and json.loads(audits[0]['outcome']) == result
    with pytest.raises(ValueError, match='EXACT_BLOCKED_LOOKUP_REQUIRED'):
        op.apply(store, plan, refresh)
    assert len(store.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))) == 1


def test_changed_plan_cannot_resume_or_erase_history(case):
    _, store, _, plan, refresh = case
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode({'changed': True}), op.LOOKUP))
    with pytest.raises(ValueError, match='LOOKUP_PLAN_CHANGED'):
        op.apply(store, plan, refresh)
    assert store.get(op.LOOKUP)['state'] == 'awaiting_review'
    assert store.get(op.LOOKUP)['result'] == {'changed': True}
    assert not store.query('SELECT * FROM audit WHERE action=?', (op.VERSION,))


def test_live_lease_and_unpaused_admission_block_snapshot(case):
    _, store, _, _, refresh = case
    with store.transaction() as db:
        db.execute('UPDATE jobs SET lease_owner=? WHERE job_id=?', ('live-owner', op.LOOKUP))
    with pytest.raises(ValueError, match='VIDEO_WORK_STILL_ACTIVE'):
        refresh()
    with store.transaction() as db:
        db.execute('UPDATE jobs SET lease_owner=NULL WHERE job_id=?', (op.LOOKUP,))
        db.execute('UPDATE pipelines SET paused=0 WHERE name=\'video\'')
    with pytest.raises(ValueError, match='VIDEO_ADMISSION_MUST_BE_PAUSED'):
        refresh()


def test_unqualified_runtime_or_changed_submit_intent_blocks(case):
    settings, store, expected, _, refresh = case
    file = settings.root / op.FILES[0]
    original = file.read_bytes()
    file.write_bytes(b'changed source')
    with pytest.raises(ValueError, match='QUALIFIED_RUNTIME_CHANGED'):
        refresh()
    file.write_bytes(original)
    with store.transaction() as db:
        db.execute('UPDATE replay_part_lookup_schedule_v5 SET intent_sha256=?', ('b' * 64,))
    with pytest.raises(ValueError, match='EXACT_SCHEDULE_REQUIRED'):
        refresh()
