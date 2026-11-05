"""Owned SQLite and real FFmpeg/checker; no account, platform or fake QA."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import subprocess
import time

import pytest

from test_short_video_web import short_video, PRINCIPAL
from test_replay_video_web import replay
from test_replay_machine_checks import owned
from test_replay_native_snapshot_delta import native, put_native
from services.hub.errors import HubError
from services.hub.db import encode
from scripts.ops.resume_unconsumed_replay_reservation_v1 import (
    ReuseUnconsumedReservation, OperationGuard, publications, validate_original, validate_manager)


@pytest.fixture
def recovery(native):
    f = native; old = f['service']; ident = f['job_id']
    old.record_native_prepare(PRINCIPAL, ident, f['observation'])
    first = old.reserve_submit(PRINCIPAL, ident, attempt_id=f['attempt'],
        site_uid=f['db'].get(ident)['payload']['site_uid'], upload_reference_sha256='b'*64)
    old.mark_unknown(PRINCIPAL, ident)
    with f['db'].transaction() as db:
        db.execute('UPDATE jobs SET attempts=3 WHERE job_id=?', (ident,))
    class Service(ReuseUnconsumedReservation, type(old)): pass
    service = Service(f['s'], f['db'])
    job, _, state = service.snapshot(ident, write=False)
    pub = publications(f['db'], job)
    validate_original(job, state, pub)
    folder = f['s'].root/'media'/ident/'replay-native-intents'; folder.mkdir()
    service.recovery_intents = folder
    service.recovery_operation = 'owned-resume-not-a-platform-action'
    service.recovery_publication = deepcopy(pub[0])
    service.recovery_check = lambda: None
    service.recovery_used = False
    service._update(PRINCIPAL, job, {**state, 'state': 'uploaded',
        'reservation_recovery': {'operation_id': service.recovery_operation, 'previous_checkpoint': deepcopy(state)}},
        job_state='publish_unknown', stage='owned_observed_private_draft')
    f.update(service=service, first_reservation=first, original_publication=pub[0])
    return f


def reserve(f):
    return f['service'].reserve_submit(PRINCIPAL, f['job_id'], attempt_id=f['attempt'],
        site_uid=f['db'].get(f['job_id'])['payload']['site_uid'], upload_reference_sha256='b'*64)


def test_real_two_qa_reuse_same_row_key_and_preserve_attempts_no_second_reservation(recovery):
    f = recovery; service = f['service']; ident = f['job_id']
    service.record_native_prepare(PRINCIPAL, ident, f['observation'])
    result = reserve(f)
    assert result['can_click_submit'] is True and result['idempotency_key'] == f['first_reservation']['idempotency_key']
    assert service.recovery_used is True
    job = f['db'].get(ident); pubs = publications(f['db'], job)
    assert len(pubs) == 1 and pubs[0]['idempotency_key'] == f['original_publication']['idempotency_key']
    assert pubs[0]['state'] == 'dispatching' and job['attempts'] == 3
    assert job['state'] == 'publish_unknown' and job['lease_owner'] is job['lease_until'] is None
    assert job['result'][service.STATE]['attempt_id'] == f['attempt']
    assert job['result'][service.STATE]['state'] == 'submitting'
    assert len(f['db'].query("SELECT 1 FROM audit WHERE action='short_video_reserve_submit' AND target=?", (ident,))) == 1
    assert len(f['db'].query("SELECT 1 FROM audit WHERE action='replay_unconsumed_reservation_reused' AND target=?", (ident,))) == 1
    with pytest.raises(HubError): reserve(f)
    assert len(publications(f['db'], f['db'].get(ident))) == 1


def test_current_same_size_media_corruption_still_fails_real_qa(recovery):
    f = recovery; raw = bytearray(f['file'].read_bytes()); raw[-100] ^= 1; f['file'].write_bytes(raw)
    with pytest.raises(HubError): f['service'].record_native_prepare(PRINCIPAL, f['job_id'], f['observation'])
    assert publications(f['db'], f['db'].get(f['job_id'])) == [f['original_publication']]
    assert f['service'].recovery_used is False


def test_native_freshness_stays_300_seconds_even_after_real_qa(recovery):
    f = recovery; old = deepcopy(f['observation'])
    old['observed_at'] = (datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
    expired = put_native(f, old)
    with pytest.raises(HubError, match='NATIVE_PREPARATION_REQUIRED'):
        f['service'].record_native_prepare(PRINCIPAL, f['job_id'], expired)
    assert publications(f['db'], f['db'].get(f['job_id'])) == [f['original_publication']]


@pytest.mark.parametrize('change', ['public_intent', 'changed_publication'])
def test_original_reservation_cas_and_native_intent_refuse_reuse(recovery, change):
    f = recovery
    if change == 'public_intent':
        (f['service'].recovery_intents/'public-submit.json').write_text('Owned prior intent, never a real submit', 'utf-8')
    else:
        with f['db'].transaction() as db:
            db.execute("UPDATE publications SET updated_at=updated_at+1 WHERE job_id=?", (f['job_id'],))
    before = publications(f['db'], f['db'].get(f['job_id']))
    with pytest.raises(AssertionError): reserve(f)
    assert publications(f['db'], f['db'].get(f['job_id'])) == before
    assert f['service'].recovery_used is False


def test_protection_callback_blocks_before_reusing_original_reservation(recovery):
    f = recovery
    def stop(): raise HubError('OWNED_PROTECTION_REQUIRED', review=True)
    f['service'].recovery_check = stop
    with pytest.raises(HubError, match='OWNED_PROTECTION_REQUIRED'): reserve(f)
    assert publications(f['db'], f['db'].get(f['job_id'])) == [f['original_publication']]


def test_manager_requires_actual_loaded_empty_all_tab_not_zero_early_links():
    # APP must bind against the actual frozen Highest-v2 helper as well as
    # the later optional Natural path: no unqualified directory keyword.
    import ast
    import inspect
    from services.hub.replay_highest_coverage_v2 import upload_coverage
    from scripts.ops.resume_unconsumed_replay_reservation_v1 import app_main
    calls = [n for n in ast.walk(ast.parse(inspect.getsource(app_main)))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'upload_coverage']
    assert len(calls) == 1
    kwargs = {item.arg: ast.literal_eval(item.value) for item in calls[0].keywords}
    inspect.signature(upload_coverage).bind(*([None]*len(calls[0].args)), **kwargs)
    assert kwargs == {'fresh': False}
    text = '视频管理\n图文管理\n互动视频管理\n全部稿件 0\n草稿 2\n进行中 0\n已发布 0\n未通过 0\n一个稿件都没有，请换个筛选条件'
    inventory = {'visible_dom': {'url': 'https://member.bilibili.com/platform/upload-manager/article',
                                'visible_text': text, 'links': []}}
    meta = {'title': 'Owned exact title', 'source_url': 'https://www.youtube.com/watch?v=ownedvideo1'}
    validate_manager({'state': 'unknown'}, inventory, meta)
    for bad_text, links, result in [('', [], {'state': 'unknown'}),
            (text.replace('全部稿件 0', '全部稿件 1'), [], {'state': 'unknown'}),
            (text+'\n'+meta['title'], [], {'state': 'unknown'}),
            (text, [{'url': 'https://www.bilibili.com/video/BV1234567890'}], {'state': 'unknown'}),
            (text, [], {'state': 'unknown', 'external_id': 'BV1234567890'})]:
        altered = deepcopy(inventory); altered['visible_dom'].update(visible_text=bad_text, links=links)
        with pytest.raises(AssertionError): validate_manager(result, altered, meta)


def test_operation_guard_initializes_real_intent_hashes_and_fences_deadline_and_row(recovery):
    f = recovery; job = f['db'].get(f['job_id'])
    guard = OperationGuard(f['s'], f['db'], job, publications(f['db'], job), f['service'].recovery_intents, {})
    guard.state_key = f['service'].STATE
    assert guard.original_intents == {}
    guard()
    with f['db'].transaction() as db:
        db.execute('UPDATE jobs SET attempts=attempts+1 WHERE job_id=?', (f['job_id'],))
    with pytest.raises(AssertionError): guard()
    with f['db'].transaction() as db:
        db.execute('UPDATE jobs SET attempts=attempts-1 WHERE job_id=?', (f['job_id'],))
    guard()
    guard.deadline = time.monotonic()-1
    with pytest.raises(AssertionError): guard()


@pytest.mark.parametrize('scenario', ['delayed_ready', 'real_challenge', 'public_candidate'])
def test_operator_manager_passive_query_real_chromium(tmp_path, scenario):
    repo = Path(__file__).resolve().parents[1]
    env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'SYSTEMROOT', 'SystemDrive', 'SYSTEMDRIVE',
        'ProgramData', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE',
        'TEMP', 'TMP', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'LANG', 'LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(repo/'compositor/runtime/browsers')
    result = subprocess.run(['node', str(repo/'tests/replay_unconsumed_manager_query_fixture.mjs'),
                              str(tmp_path), scenario], cwd=repo, env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=35)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    assert proof['scenario'] == scenario and proof['live_tested'] is False
    assert proof['native_clicks'] == proof['file_selections'] == proof['external_requests'] == 0
