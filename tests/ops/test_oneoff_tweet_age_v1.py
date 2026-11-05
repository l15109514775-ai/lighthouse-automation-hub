"""Owned SQLite/authorization/transport only; source/render/rootfs are boundaries.

No real source, model, account, root-created grant or native send is asserted by
these tests. Real generation, tombstone, SQLite CAS and one-use ledgers remain.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import time
import uuid

import pytest

from scripts.ops import oneoff_tweet_age_v1 as m
from services.hub.db import encode
from services.hub.errors import HubError, PublishUnknown


def pair(now=None):
    now = time.time() if now is None else now
    plan = {'created_at': now-10, 'artifact_sha256': 'a'*64, 'caption_sha256': 'b'*64,
            'job_policy_version': m.MachineTweetsAPI.version, 'payload_sha256': 'c'*64,
            'policy_sha256': 'd'*64, 'manifest': {'sha256': 'e'*64}}
    ref = {'file': '/owned/plan.json', 'sha256': 'f'*64, 'bytes': 111}
    grant = {'version': m.GRANT_VERSION, 'grant_id': str(uuid.uuid4()), 'job_id': m.JOB_ID,
             'source_id': m.SOURCE_ID, 'target_site_uid': m.SITE_UID, 'owner_response': m.OWNER_RESPONSE,
             'owner_response_reference': 'owned actual-response reference only', 'issued_at': now-5,
             'expires_at': now+300, 'plan_file': ref['file'], 'plan_sha256': ref['sha256'],
             'manifest_sha256': plan['manifest']['sha256'], 'max_dispatches': 1,
             'scope': ['ignore_source_post_age_for_this_job_once'], 'human_review': False,
             **{key: plan[key] for key in ('artifact_sha256', 'caption_sha256', 'job_policy_version', 'payload_sha256', 'policy_sha256')}}
    assert set(grant) == m.GRANT_KEYS
    return grant, plan, ref


def test_exact_one_job_grant_is_live_without_claiming_human_review():
    grant, plan, ref = pair();before = deepcopy((grant, plan, ref))
    assert m.validate_grant(grant, plan, ref, require_live=True) is None
    assert (grant, plan, ref) == before and grant['human_review'] is False


@pytest.mark.parametrize('change', ['expired', 'target', 'human', 'more_dispatches', 'artifact'])
def test_grant_expiry_target_review_and_exact_artifact_cannot_be_relaxed(change):
    grant, plan, ref = pair()
    if change == 'expired':
        grant['expires_at'] = time.time()-1
        # Lookup/reconciliation may inspect an expired authentic grant.
        m.validate_grant(grant, plan, ref, require_live=False)
    elif change == 'target':grant['target_site_uid'] = 'owned-wrong-account'
    elif change == 'human':grant['human_review'] = True
    elif change == 'more_dispatches':grant['max_dispatches'] = 2
    else:grant['artifact_sha256'] = '0'*64
    with pytest.raises(HubError):m.validate_grant(grant, plan, ref, require_live=True)


def test_deadline_plan_receipt_and_manifest_are_all_strictly_bound(runtime):
    grant, plan, ref = pair()
    for field, value in (('expires_at', grant['issued_at']+3601), ('plan_file', '/owned/other.json'),
                         ('plan_sha256', '0'*64), ('manifest_sha256', '0'*64), ('max_dispatches', True)):
        changed = deepcopy(grant);changed[field] = value
        with pytest.raises(HubError):m.validate_grant(changed, plan, ref)
    settings, _ = runtime;ident = str(uuid.uuid4())
    record = {**plan, 'version': m.PLAN_VERSION, 'plan_id': ident, 'job_id': m.JOB_ID,
              'source_id': m.SOURCE_ID, 'target_site_uid': m.SITE_UID, 'original_state': 'failed',
              'original_error_code': 'MACHINE_COMPOSITOR_REAL_CHECK_FAILED', 'original_attempts': 1,
              'model_job_id': m.MODEL_JOB_ID, 'human_review': False, 'publication_authorized': False,
              'job_unchanged': True, 'paid_model_calls': 0, 'caption': '【补发】Owned isolated plan'}
    file = settings.root/'media'/m.JOB_ID/'oneoff-age-v1'/ident/'plan.json'
    actual = m.write_new(file, record)
    assert m.load_plan(settings, file, actual['sha256']) == (record, actual)
    with pytest.raises(HubError, match='PLAN_HASH_CHANGED'):m.load_plan(settings, file, '0'*64)


@pytest.fixture
def prepared(runtime, monkeypatch):
    settings, store = runtime
    settings.config.update(target_bilibili_site_uid=m.SITE_UID, policy_version=m.MachineTweetsAPI.version)
    payload = {'post': {'id': m.SOURCE_ID, 'text': 'Owned old source fixture',
                       'created_at': datetime.fromtimestamp(time.time()-10800, timezone.utc).isoformat()},
               'author': {'username': 'example-channel-b'}, 'url': 'https://x.com/example-channel-b/status/'+m.SOURCE_ID}
    ident = store.enqueue('x', m.SOURCE_ID, 'tweet_process', 'tweet', payload, m.MachineTweetsAPI.version)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET job_id=?,state='failed',attempts=1,error_code='MACHINE_COMPOSITOR_REAL_CHECK_FAILED',stage='owned-original-render-error',result=? WHERE job_id=?",
                   (m.JOB_ID, encode({'owned_old_error_evidence': {'code': 'GLYPH_MISSING', 'human_review': False}}), ident))
    original = store.get(m.JOB_ID);policy = {'max_post_age_seconds': 7200, 'owned_policy_boundary': True}
    caption = '【补发】Owned fixture '+payload['url']
    manifest = {'source_sha256': '1'*64, 'render': {'sha256': 'a'*64, 'png': str(settings.root/'media'/m.JOB_ID/'owned.png')},
                'caption': caption, 'caption_sha256': hashlib.sha256(caption.encode()).hexdigest(),
                'policy_sha256': m.digest(policy), 'source_url': payload['url'], 'target_site_uid': m.SITE_UID,
                'target_name': 'Owned target'}
    file = settings.root/'media'/m.JOB_ID/'owned-manifest.json';manifest_ref = m.write_new(file, manifest)
    grant, plan, ref = pair()
    plan.update(before_row_sha256=m.row_sha(original), original_error_code=original['error_code'], original_attempts=1,
                original_created_at=payload['post']['created_at'], payload_sha256=m.digest(payload),
                policy_sha256=m.digest(policy), manifest=manifest_ref, artifact_sha256=manifest['render']['sha256'],
                caption_sha256=manifest['caption_sha256'], caption=caption)
    grant.update(manifest_sha256=manifest_ref['sha256'], **{key: plan[key] for key in
        ('payload_sha256', 'policy_sha256', 'artifact_sha256', 'caption_sha256')})
    grant_ref = {'file': str(settings.root/'config/oneoff-tweet-age-v1/grant.json'), 'sha256': '2'*64, 'bytes': 222}
    # Root protection is tested by the root operator separately. This explicit
    # boundary still uses actual strict grant/schema checks in every invocation.
    def protected(s, file, expected, p, r, *, require_live=False):
        assert file == settings.root/'config/oneoff-tweet-age-v1/grant.json' and expected == grant_ref['sha256']
        m.validate_grant(grant, p, r, require_live=require_live)
        return grant, grant_ref
    monkeypatch.setattr(m, 'protected_grant', protected)
    monkeypatch.setattr(m.MachineTweetsAPI, 'policy', lambda self: deepcopy(policy))
    # Snapshot/source authenticity is an expressly isolated upstream boundary;
    # no generated fake render or model proof is accepted by production here.
    monkeypatch.setattr(m.MachineTweetsAPI, '_snapshot', lambda self, job_id:(self.store.get(job_id), deepcopy(manifest)))
    monkeypatch.setattr(m.MachineTweetsAPI, 'source_check', lambda self, job, **kw: {'owned_upstream_boundary': True})
    service = m.OneoffMachineTweetsAPI(settings, store, plan=plan, plan_ref=ref,
        grant_file=settings.root/'config/oneoff-tweet-age-v1/grant.json', grant_sha256=grant_ref['sha256'])
    return settings, store, service, original, grant, manifest


def test_adopt_prepared_real_sqlite_preserves_attempt_timestamp_error_history_and_is_idempotent(prepared):
    settings, store, service, original, grant, manifest = prepared
    assert m.adopt_prepared(service) == {'registered': True, 'already_registered': False}
    current = store.get(m.JOB_ID);history = current['result']['oneoff_owner_age_exception']
    assert current['state'] == 'upload_pending' and current['error_code'] is None
    assert current['attempts'] == original['attempts'] == 1 and current['payload'] == original['payload']
    assert current['created_at'] == original['created_at'] and current['result']['owned_old_error_evidence'] == original['result']['owned_old_error_evidence']
    assert history['original_error_code'] == 'MACHINE_COMPOSITOR_REAL_CHECK_FAILED' and history['human_review'] is False
    assert m.adopt_prepared(service) == {'registered': False, 'already_registered': True}
    assert len(store.query("SELECT * FROM audit WHERE action='register_single_tweet_age_grant'")) == 1
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')
    # The real inherited reservation transaction consumes one source-wide
    # dispatch. Only upstream source/snapshot authenticity is isolated above.
    args = {'site_uid': m.SITE_UID, 'artifact_sha256': manifest['render']['sha256'],
            'caption_sha256': manifest['caption_sha256']}
    assert service.reserve('machine-policy', m.JOB_ID, **args)['can_click_publish'] is True
    assert service.reserve('machine-policy', m.JOB_ID, **args)['can_click_publish'] is False
    assert len(store.query('SELECT * FROM publications')) == 1
    assert len(store.query("SELECT * FROM audit WHERE action='consume_single_tweet_age_grant'")) == 1
    assert store.get(m.JOB_ID)['attempts'] == 1


@pytest.mark.parametrize('change', ['publication', 'tombstone', 'cas'])
def test_adopt_rejects_existing_publication_owner_clear_and_concurrent_row_change(prepared, monkeypatch, change):
    settings, store, service, original, grant, manifest = prepared
    if change == 'publication':
        with store.transaction() as db:
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('owned-unknown', m.JOB_ID, 'bilibili', 'x', m.SOURCE_ID, 'site_uid:'+m.SITE_UID, service.kind, 'unknown', None, None, time.time()))
    elif change == 'tombstone':
        with store.transaction() as db:
            db.execute('INSERT INTO source_tombstones VALUES(?,?,?,?,?,?)',
                ('x', m.SOURCE_ID, 'OWNER_BACKLOG_CLEARED', time.time(), m.JOB_ID, 'owner_clear_all'))
    else:
        def race(job, **kwargs):
            with store.transaction() as db:db.execute('UPDATE jobs SET updated_at=updated_at+1 WHERE job_id=?', (m.JOB_ID,))
        monkeypatch.setattr(service, 'source_check', race)
    with pytest.raises(HubError):m.adopt_prepared(service)
    assert store.get(m.JOB_ID)['state'] == 'failed' and store.get(m.JOB_ID)['attempts'] == 1
    assert not store.query("SELECT * FROM audit WHERE action='register_single_tweet_age_grant'")


def test_expired_oneoff_unknown_transport_only_looks_up_and_never_reserves(prepared, monkeypatch):
    settings, store, service, original, grant, manifest = prepared
    grant['expires_at'] = time.time()-1
    with store.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-unknown', m.JOB_ID, 'bilibili', 'x', m.SOURCE_ID, 'site_uid:'+m.SITE_UID, service.kind, 'unknown', None, None, time.time()))
    observed = []
    class Input(io.BytesIO):
        def close(self):
            spec = json.loads(self.getvalue());observed.append(spec)
            folder = settings.root/'media'/m.JOB_ID/'website-attempts'/spec['attempt_id']
            (folder/'result.json').write_text(encode({'state': 'not_found_result_unknown', 'sent': False}), 'utf-8')
            super().close()
    class Process:
        def __init__(self, *a, **kw):self.stdin = Input()
        def poll(self):return 0
    monkeypatch.setattr(m.subprocess, 'Popen', Process)
    monkeypatch.setattr(service, 'reserve', lambda *a, **kw: pytest.fail('Unknown must not reserve again'))
    with pytest.raises(PublishUnknown):m.run_oneoff_website(service)
    assert len(observed) == 1 and observed[0]['mode'] == 'lookup' and observed[0]['dispatch_expires_at'] < time.time()
    assert store.query('SELECT state FROM publications')[0]['state'] == 'unknown'
    assert not store.query("SELECT * FROM audit WHERE action='consume_single_tweet_age_grant'")
    assert store.get(m.JOB_ID)['attempts'] == 1 and not store.query('SELECT * FROM api_calls')
