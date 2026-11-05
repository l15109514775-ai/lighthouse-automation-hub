"""Owned in-memory/temporary-file evidence only; no real upload or human claims."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.short_video_web import ShortVideoWeb, POLICY, PRINCIPAL, STATE, KIND, JOB_TYPE
from services.hub.youtube_download import VERSION as DOWNLOAD_VERSION, STATE as DOWNLOAD_STATE, digest

BV = 'BV0000000000'  # Fictional fixture; never sent to a real site.


@pytest.fixture
def short_video(runtime):
    s, db = runtime
    s.config.update(target_bilibili_site_uid='123456', target_bilibili_account_name='Owned synthetic account')
    s.root.joinpath('config').mkdir(exist_ok=True)
    policy = {'version': POLICY, 'enabled': True, 'target_site_uid': '123456', 'target_account_name': 'Owned synthetic account',
              'public_submission_mode': 'per_artifact_owner_acceptance', 'owner_authorization_reference': 'Owned fixture only',
              'owner_permission_reference': 'Owned synthetic media fixture', 'acceptance_video_ids': ['abcdefghijk'],
              'activated_at': '2026-01-01T00:00:00Z'}
    policy_file = s.root / 'config/short-video-web-policy.toml'
    policy_file.write_text('\n'.join(k+' = '+json.dumps(v) for k,v in policy.items())+'\n', 'utf-8')
    payload = {'video_id': 'abcdefghijk', 'channel_id': 'UCaaaaaaaaaaaaaaaaaaaaaa',
               'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'title': 'Owned synthetic source'}
    source = db.enqueue('youtube', 'abcdefghijk', 'youtube_download', 'video', payload, 'owned-download-fixture')
    leased = db.claim('video', 180, 4)
    assert leased['job_id'] == source
    folder = s.root / 'media' / source / 'youtube-download-v1'; folder.mkdir(parents=True)
    file = folder / 'original.mp4'; file.write_bytes(b'Owned fixture bytes; not a real source video or live verification')
    asset = {'file': str(file), 'bytes': file.stat().st_size, 'sha256': sha256_file(file), 'duration': 297,
             'stream_types': {'0': 'video', '1': 'audio'}}
    binding = {'job_id': source, 'video_id': 'abcdefghijk', 'channel_id': payload['channel_id'],
               'source_payload_sha256': digest(payload), 'duration': 297}
    receipt = {'source_to_merged_packet_coverage_verified': True, 'owned_fixture_only': True}
    verified = {'version': DOWNLOAD_VERSION, 'binding_sha256': digest(binding), 'source_asset': asset, 'receipt': receipt,
                'human_review': False, 'publication_authorized': False, 'creator_original_bytes_verified': False}
    (folder / 'verified.json').write_text(encode(verified), 'utf-8')
    result = {DOWNLOAD_STATE: {'version': DOWNLOAD_VERSION, 'state': 'verified', 'binding': binding,
                              'source_asset': asset, 'receipt': receipt, 'human_review': False, 'publication_authorized': False},
              'download_performed': True, 'publish_performed': False, 'human_review': False, 'publication_authorized': False}
    db.finish(leased, 'completed', result=result)
    metadata = {'title': 'Owned short fixture', 'description': 'Owned fixture source '+payload['url'],
                'tags': ['owned', 'fixture'], 'category_id': 65, 'copyright': 2, 'source': payload['url']}
    return s, db, ShortVideoWeb(s, db), source, metadata, policy_file


def create(f):
    _, _, service, source, metadata, _ = f
    return service.create_acceptance(PRINCIPAL, source, metadata)


def upload(f, *, approved=False):
    _, db, service, _, _, _ = f
    job = create(f); attempt = str(uuid.uuid4()); binding = db.get(job)['payload']
    service.start_upload(PRINCIPAL, job, attempt_id=attempt, site_uid='123456')
    obs = {'version': 'short-video-browser-upload-v1', 'attempt_id': attempt, 'site_uid': '123456',
           'artifact_sha256': binding['artifact_sha256'], 'file_bytes': binding['file_bytes'],
           'upload_reference_sha256': 'b'*64, 'same_attempt_observed': True, 'server_file_hash_verified': False}
    service.record_upload(PRINCIPAL, job, obs)
    if approved:
        service.approve(PRINCIPAL, job, binding_sha256=digest(binding), actual_owner_response='Owned fixture acceptance; not human review',
                        approval_reference='Owned synthetic authorization fixture')
    return job, attempt, binding


def reserve(f):
    _, _, service, _, _, _ = f
    job, attempt, binding = upload(f, approved=True)
    permit = service.reserve_submit(PRINCIPAL, job, attempt_id=attempt, site_uid='123456', upload_reference_sha256='b'*64)
    assert permit['can_click_submit'] is True
    return job, attempt, binding


def result(attempt, binding, state='awaiting_moderation'):
    return {'version': 'short-video-browser-result-v1', 'attempt_id': attempt, 'site_uid': '123456',
            'source_url': binding['source_url'], 'metadata_sha256': binding['metadata_sha256'],
            'external_id': BV, 'url': 'https://www.bilibili.com/video/'+BV,
            'state': state, 'same_submit_attempt_observed': True}


def test_verified_short_preparation_is_idempotent_and_not_a_download_or_human_approval(short_video):
    _, db, service, source, _, _ = short_video
    job = create(short_video)
    assert create(short_video) == job
    assert db.get(source)['state'] == 'completed'
    current = db.get(job)
    assert current['job_type'] == JOB_TYPE and current['state'] == 'awaiting_review'
    assert current['result'][STATE]['human_review'] is False
    assert current['result'][STATE]['public_submission_approved'] is False
    assert current['payload']['human_review'] is False
    assert not db.query('SELECT * FROM publications')
    assert not db.query('SELECT * FROM publication_reviews')
    assert db.claim('video', 180, 4) is None  # No unknown Worker handler is triggered.


@pytest.mark.parametrize('field,value', [('enabled', False), ('version', 'old'), ('target_site_uid', 'other'),
    ('target_account_name', 'other'), ('public_submission_mode', 'automatic'), ('owner_authorization_reference', ''),
    ('owner_permission_reference', ''), ('activated_at', '2099-01-01T00:00:00Z'), ('acceptance_video_ids', [])])
def test_policy_defaults_and_unauthorized_scope_stop_before_upload(short_video, field, value):
    _, db, _, _, _, path = short_video
    import tomllib
    p = tomllib.loads(path.read_text('utf-8')); p[field] = value
    path.write_text('\n'.join(k+' = '+json.dumps(v) for k,v in p.items()), 'utf-8')
    with pytest.raises(HubError, match='SHORT_VIDEO_POLICY_REQUIRED'):
        create(short_video)
    assert db.query("SELECT * FROM jobs WHERE job_type=?", (JOB_TYPE,)) == []


def test_missing_policy_is_disabled(short_video):
    short_video[-1].unlink()
    with pytest.raises(HubError, match='SHORT_VIDEO_POLICY_REQUIRED'):
        create(short_video)


@pytest.mark.parametrize('principal', ['maintenance', 'reader', '', 'machine-policy'])
def test_only_local_operator_write_scope_is_accepted(short_video, principal):
    _, db, service, source, metadata, _ = short_video
    with pytest.raises(HubError, match='ACCESS_DENIED'):
        service.create_acceptance(principal, source, metadata)
    assert not db.query('SELECT * FROM publications')


@pytest.mark.parametrize('field,value', [('title', ''), ('description', 'missing source'), ('copyright', 1),
    ('source', 'https://example.test/other'), ('tags', []), ('tags', ['same', 'same']), ('tags', ['bad,tag']),
    ('category_id', 0), ('category_id', True)])
def test_metadata_is_bound_and_repost_source_is_required(short_video, field, value):
    short_video[4][field] = value
    with pytest.raises(HubError, match='SHORT_VIDEO_METADATA_INVALID'):
        create(short_video)


def test_visible_category_label_is_preserved_and_exactly_bound(short_video):
    _, db, service, _, metadata, _ = short_video
    metadata.pop('category_id')
    metadata['category_label'] = 'Owned visible category'
    job = create(short_video)
    _, binding, _ = service.snapshot(job)
    assert binding['metadata'] == metadata
    assert binding['metadata']['category_label'] == 'Owned visible category'
    assert 'category_id' not in binding['metadata']
    assert binding['metadata_sha256'] == digest(metadata)
    assert db.get(job)['payload']['metadata_sha256'] == digest(metadata)
    assert create(short_video) == job


def test_different_category_labels_change_the_bound_metadata(short_video):
    metadata = short_video[4]
    metadata.pop('category_id')
    metadata['category_label'] = 'Owned visible category A'
    job = create(short_video)
    original = deepcopy(short_video[1].get(job)['payload'])
    metadata['category_label'] = 'Owned visible category B'
    checked = short_video[2]._metadata(metadata, metadata['source'])
    assert digest(checked) != original['metadata_sha256']
    with pytest.raises(HubError, match='SHORT_VIDEO_ACCEPTANCE_INPUT_CHANGED'):
        create(short_video)
    assert short_video[1].get(job)['payload'] == original


@pytest.mark.parametrize('label', ['', '  ', '\t\n', 'x' * 41, None, 65, True, []])
def test_invalid_category_label_is_rejected(short_video, label):
    metadata = short_video[4]
    metadata.pop('category_id')
    metadata['category_label'] = label
    with pytest.raises(HubError, match='SHORT_VIDEO_METADATA_INVALID'):
        create(short_video)


def test_category_id_and_label_cannot_be_supplied_together(short_video):
    short_video[4]['category_label'] = 'Owned visible category'
    with pytest.raises(HubError, match='SHORT_VIDEO_METADATA_INVALID'):
        create(short_video)


def test_category_label_contract_does_not_normalize_the_hashed_input(short_video):
    metadata = short_video[4]
    metadata.pop('category_id')
    metadata['category_label'] = ' Owned visible category '
    checked = short_video[2]._metadata(metadata, metadata['source'])
    assert checked == metadata and checked is not metadata
    assert digest(checked) != digest({**metadata, 'category_label': metadata['category_label'].strip()})


@pytest.mark.parametrize('change', ['bytes', 'duration', 'receipt', 'human', 'source_payload', 'file'])
def test_changed_or_long_download_does_not_become_uploadable(short_video, change):
    _, db, _, source, _, _ = short_video
    job = db.get(source); value = deepcopy(job['result']); asset = value[DOWNLOAD_STATE]['source_asset']
    if change == 'bytes': asset['bytes'] = 100_000_001
    elif change == 'duration': asset['duration'] = 601
    elif change == 'receipt': (Path(asset['file']).parent/'verified.json').write_text('{}', 'utf-8')
    elif change == 'human': value['human_review'] = True
    elif change == 'source_payload':
        with db.transaction() as conn:
            conn.execute('UPDATE jobs SET payload=? WHERE job_id=?', (encode({'video_id': 'changed'}), source))
    else: Path(asset['file']).write_bytes(b'Changed owned fixture')
    if change != 'source_payload':
        with db.transaction() as conn:
            conn.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(value), source))
    with pytest.raises(HubError): create(short_video)


def test_changed_metadata_cannot_silently_reuse_old_acceptance(short_video):
    job = create(short_video)
    short_video[4]['title'] = 'Changed owned title'
    with pytest.raises(HubError, match='SHORT_VIDEO_ACCEPTANCE_INPUT_CHANGED'):
        create(short_video)
    assert short_video[1].get(job)['payload']['metadata']['title'] == 'Owned short fixture'


def test_private_upload_remains_unpublished_without_explicit_artifact_acceptance(short_video):
    _, db, service, _, _, _ = short_video
    job, attempt, _ = upload(short_video)
    with pytest.raises(HubError, match='SHORT_VIDEO_EXPLICIT_ARTIFACT_ACCEPTANCE_REQUIRED'):
        service.reserve_submit(PRINCIPAL, job, attempt_id=attempt, site_uid='123456', upload_reference_sha256='b'*64)
    assert db.get(job)['state'] == 'upload_pending'
    assert db.get(job)['result'][STATE]['state'] == 'uploaded'
    assert not db.query('SELECT * FROM publications')


def test_same_attempt_observation_never_claims_remote_file_hash_or_stores_token(short_video):
    _, db, _, _, _, _ = short_video
    job, _, _ = upload(short_video)
    state = db.get(job)['result'][STATE]
    assert state['upload_observation']['server_file_hash_verified'] is False
    assert 'upload_reference' not in state['upload_observation']
    assert state['human_review'] is False


@pytest.mark.parametrize('field,value', [('site_uid', 'wrong'), ('attempt_id', str(uuid.uuid4())),
    ('file_bytes', 999), ('artifact_sha256', '0'*64), ('server_file_hash_verified', True), ('same_attempt_observed', False),
    ('upload_reference_sha256', 'raw-private-upload-reference')])
def test_upload_receipt_cannot_change_identity_or_claim_impossible_checks(short_video, field, value):
    _, db, service, _, _, _ = short_video
    job = create(short_video); attempt = str(uuid.uuid4()); b = db.get(job)['payload']
    service.start_upload(PRINCIPAL, job, attempt_id=attempt, site_uid='123456')
    obs = {'version': 'short-video-browser-upload-v1', 'attempt_id': attempt, 'site_uid': '123456',
           'artifact_sha256': b['artifact_sha256'], 'file_bytes': b['file_bytes'], 'upload_reference_sha256': 'b'*64,
           'same_attempt_observed': True, 'server_file_hash_verified': False}
    obs[field] = value
    with pytest.raises(HubError, match='SHORT_VIDEO_UPLOAD_OBSERVATION_INVALID'):
        service.record_upload(PRINCIPAL, job, obs)


def test_upload_unknown_after_process_exit_requires_lookup_not_selection_again(short_video):
    s, db, service, _, _, _ = short_video
    job = create(short_video); attempt = str(uuid.uuid4())
    service.start_upload(PRINCIPAL, job, attempt_id=attempt, site_uid='123456')
    reopened = ShortVideoWeb(s, Store(s.db_path)); reopened.mark_unknown(PRINCIPAL, job)
    assert db.get(job)['result'][STATE]['state'] == 'upload_unknown'
    with pytest.raises(HubError, match='SHORT_VIDEO_UPLOAD_RECONCILE_REQUIRED'):
        reopened.start_upload(PRINCIPAL, job, attempt_id=str(uuid.uuid4()), site_uid='123456')
    assert not db.query('SELECT * FROM publications')


def test_submit_intent_is_persistent_before_click_and_can_be_consumed_once(short_video):
    s, db, service, _, _, _ = short_video
    job, attempt, _ = reserve(short_video)
    assert db.get(job)['state'] == 'publish_unknown'
    pub = db.query('SELECT * FROM publications')[0]
    assert pub['state'] == 'dispatching' and pub['kind'] == KIND
    reopened = ShortVideoWeb(s, Store(s.db_path))
    with pytest.raises(HubError):
        reopened.reserve_submit(PRINCIPAL, job, attempt_id=attempt, site_uid='123456', upload_reference_sha256='b'*64)
    assert len(db.query('SELECT * FROM publications')) == 1
    assert not db.query('SELECT * FROM publication_reviews')


def test_unknown_submit_stays_unknown_and_is_never_retried_as_upload(short_video):
    _, db, service, _, _, _ = short_video
    job, _, _ = reserve(short_video)
    service.mark_unknown(PRINCIPAL, job)
    assert db.get(job)['state'] == 'publish_unknown'
    assert db.query('SELECT state FROM publications')[0]['state'] == 'unknown'
    with pytest.raises(HubError): service.start_upload(PRINCIPAL, job, attempt_id=str(uuid.uuid4()), site_uid='123456')


def test_same_click_bv_then_read_only_moderation_lookup(short_video):
    s, db, service, _, _, path = short_video
    job, attempt, binding = reserve(short_video)
    service.record_result(PRINCIPAL, job, result(attempt, binding))
    assert db.get(job)['state'] == 'awaiting_moderation'
    assert db.get(job)['external_publish_id'] == BV
    assert db.query('SELECT state FROM publications')[0]['state'] == 'awaiting_moderation'
    path.unlink()  # Revoked/missing write policy cannot block read-only reconciliation.
    reopened = ShortVideoWeb(s, Store(s.db_path))
    reopened.record_result(PRINCIPAL, job, result(attempt, binding, 'completed'), lookup=True)
    assert db.get(job)['state'] == 'completed'
    assert db.get(job)['result']['publish_performed'] is True
    assert db.get(job)['result'][STATE]['human_review'] is False
    assert db.query('SELECT external_id FROM publications')[0]['external_id'] == BV


def test_arbitrary_bv_cannot_be_claimed_by_lookup_without_recorded_same_click(short_video):
    _, db, service, _, _, _ = short_video
    job, attempt, binding = reserve(short_video)
    service.mark_unknown(PRINCIPAL, job)
    with pytest.raises(HubError, match='SHORT_VIDEO_RESULT_CAUSAL_BINDING_REQUIRED'):
        service.record_result(PRINCIPAL, job, result(attempt, binding, 'completed'), lookup=True)
    assert db.query('SELECT external_id FROM publications')[0]['external_id'] is None


@pytest.mark.parametrize('field,value', [('site_uid', 'wrong'), ('source_url', 'https://example.test/other'),
    ('metadata_sha256', '0'*64), ('same_submit_attempt_observed', False), ('attempt_id', str(uuid.uuid4())),
    ('url', 'https://example.test/video/BV0000000000'), ('state', 'unknown')])
def test_unrelated_or_unclear_result_cannot_be_completed(short_video, field, value):
    _, db, service, _, _, _ = short_video
    job, attempt, binding = reserve(short_video); obs = result(attempt, binding); obs[field] = value
    with pytest.raises(HubError, match='SHORT_VIDEO_RESULT_CAUSAL_BINDING_REQUIRED'):
        service.record_result(PRINCIPAL, job, obs)
    assert db.get(job)['state'] == 'publish_unknown'


def test_moderation_rejection_keeps_bv_and_never_authorizes_resubmit(short_video):
    _, db, service, _, _, _ = short_video
    job, attempt, binding = reserve(short_video); obs = result(attempt, binding, 'failed')
    obs['error_code'] = 'BILIBILI_MODERATION_REJECTED'
    service.record_result(PRINCIPAL, job, obs)
    assert db.get(job)['state'] == 'failed' and db.get(job)['external_publish_id'] == BV
    with pytest.raises(HubError): service.start_upload(PRINCIPAL, job, attempt_id=str(uuid.uuid4()), site_uid='123456')


def test_cross_transport_existing_source_blocks_new_upload(short_video):
    _, db, service, source, _, _ = short_video
    with db.transaction() as conn:
        conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     ('f'*64, source, 'bilibili', 'youtube', 'abcdefghijk', 'official:owned', 'video', 'unknown', None, None, 1))
    with pytest.raises(HubError, match='SHORT_VIDEO_EXISTING_PUBLICATION_REQUIRES_LOOKUP'):
        create(short_video)


def test_completed_bv_without_public_lookup_does_not_authorize_cleanup(short_video):
    _, _, service, _, _, _ = short_video
    job, attempt, binding = reserve(short_video)
    service.record_result(PRINCIPAL, job, result(attempt, binding, 'completed'))
    assert service.cleanup_eligibility(job)['eligible'] is False


def test_fresh_public_lookup_allows_exact_candidate_but_does_not_delete(short_video):
    _, db, service, _, _, _ = short_video
    from datetime import datetime, timezone
    job, attempt, binding = reserve(short_video)
    service.record_result(PRINCIPAL, job, result(attempt, binding, 'completed'))
    obs = result(attempt, binding, 'completed')
    obs.update(public_accessible=True, public_lookup_observed_at=datetime.now(timezone.utc).isoformat())
    service.record_result(PRINCIPAL, job, obs, lookup=True)
    eligible = service.cleanup_eligibility(job)
    assert eligible['eligible'] is True and eligible['delete_performed'] is False
    assert eligible['candidate']['file'] == binding['file'] and Path(binding['file']).exists()
    assert eligible['candidate']['sha256'] == binding['artifact_sha256']
    assert db.get(job)['result'][STATE]['human_review'] is False


def test_test_retention_separates_remote_owner_confirmation_from_local_formal_launch(short_video):
    _, db, service, _, _, _ = short_video
    from datetime import datetime, timezone
    job, attempt, binding = reserve(short_video)
    service.record_result(PRINCIPAL, job, result(attempt, binding, 'completed'))
    obs = result(attempt, binding, 'completed')
    obs.update(public_accessible=True, public_lookup_observed_at=datetime.now(timezone.utc).isoformat())
    service.record_result(PRINCIPAL, job, obs, lookup=True)
    assert service.cleanup_eligibility(job)['eligible'] is True
    before = db.get(job)
    response = service.set_test_retention_policy(PRINCIPAL, job,
        actual_owner_response='Owned fixture: keep remote post for owner confirmation; local tests until formal launch',
        reference='Owned fixture retention clarification')
    current = db.get(job)
    assert current['payload'] == before['payload'] and current['state'] == 'completed'
    assert current['external_publish_id'] == before['external_publish_id'] == BV
    assert {k:v for k,v in current['result'][STATE].items() if k != 'test_retention_policy'} == before['result'][STATE]
    assert response['hold']['remote_video_deletion_requires_new_owner_confirmation'] is True
    assert response['hold']['local_test_cleanup_requires_formal_workflow_launch'] is True
    assert service.cleanup_eligibility(job) == {'eligible':False,
        'reason':'LOCAL_TESTS_RETAINED_UNTIL_FORMAL_WORKFLOW_LAUNCH','delete_performed':False}
    assert Path(binding['file']).exists() and response['delete_performed'] is False
    again = service.set_test_retention_policy(PRINCIPAL, job,
        actual_owner_response=response['hold']['actual_owner_response'], reference=response['hold']['reference'])
    assert again == response and db.get(job)['updated_at'] == current['updated_at']


@pytest.mark.parametrize('change', ['unauthorized_principal', 'missing_owner_request', 'missing_reference'])
def test_test_retention_requires_operator_and_real_owner_request(short_video, change):
    _, db, service, _, _, _ = short_video
    job, _, _ = upload(short_video)
    before = db.get(job)
    with pytest.raises(HubError):
        service.set_test_retention_policy('reader' if change == 'unauthorized_principal' else PRINCIPAL, job,
            actual_owner_response='' if change == 'missing_owner_request' else 'Owned retention fixture',
            reference='' if change == 'missing_reference' else 'Owned reference')
    assert db.get(job)['payload'] == before['payload'] and db.get(job)['result'] == before['result']


@pytest.mark.parametrize('state', ['awaiting_moderation', 'unknown'])
def test_pending_or_unknown_never_allows_cleanup(short_video, state):
    _, _, service, _, _, _ = short_video
    job, attempt, binding = reserve(short_video)
    if state == 'unknown': service.mark_unknown(PRINCIPAL, job)
    else: service.record_result(PRINCIPAL, job, result(attempt, binding))
    assert service.cleanup_eligibility(job)['eligible'] is False


def test_public_accessibility_cannot_be_claimed_by_submit_page_result(short_video):
    _, _, service, _, _, _ = short_video
    from datetime import datetime, timezone
    job, attempt, binding = reserve(short_video)
    obs = result(attempt, binding, 'completed')
    obs.update(public_accessible=True, public_lookup_observed_at=datetime.now(timezone.utc).isoformat())
    with pytest.raises(HubError, match='SHORT_VIDEO_RESULT_CAUSAL_BINDING_REQUIRED'):
        service.record_result(PRINCIPAL, job, obs)


def test_cleanup_refuses_changed_artifact_even_after_public_lookup(short_video):
    _, _, service, _, _, _ = short_video
    from datetime import datetime, timezone
    job, attempt, binding = reserve(short_video)
    service.record_result(PRINCIPAL, job, result(attempt, binding, 'completed'))
    obs = result(attempt, binding, 'completed')
    obs.update(public_accessible=True, public_lookup_observed_at=datetime.now(timezone.utc).isoformat())
    service.record_result(PRINCIPAL, job, obs, lookup=True)
    Path(binding['file']).write_bytes(b'Changed owned fixture')
    assert service.cleanup_eligibility(job)['eligible'] is False


def private_unknown(f):
    _, _, service, _, _, _ = f
    job = create(f); attempt = str(uuid.uuid4())
    service.start_upload(PRINCIPAL, job, attempt_id=attempt, site_uid='123456')
    service.mark_unknown(PRINCIPAL, job)
    return job, attempt


def test_private_retry_preserves_unknown_history_files_and_job_attempts(short_video):
    s, db, service, _, _, _ = short_video
    job, old_attempt = private_unknown(short_video)
    folder = s.root/'media'/job/'short-video-attempts'/old_attempt; folder.mkdir(parents=True)
    proof = folder/'owned-error-inventory.json'; proof.write_text('Owned unchanged initial UI; no proof of private blob absence', 'utf-8')
    before = proof.read_bytes()
    with db.transaction() as conn: conn.execute('UPDATE jobs SET attempts=3 WHERE job_id=?', (job,))
    result = service.operator_retry_private_upload(PRINCIPAL, job, 'Owned operator checked no public intent; private blob still unknown')
    current = db.get(job); state = current['result'][STATE]
    assert result['remaining_private_selections'] == 1 and result['public_submit'] is False
    assert current['attempts'] == 3 and state['state'] == 'prepared'
    assert state['private_upload_history'][0]['attempt_id'] == old_attempt
    assert state['private_upload_history'][0]['previous_checkpoint']['state'] == 'upload_unknown'
    assert current['result']['orphan_private_blob_unknown'] is True and current['result']['public_submit'] is False
    assert proof.read_bytes() == before and not db.query('SELECT * FROM publications')
    new_attempt = str(uuid.uuid4()); service.start_upload(PRINCIPAL, job, attempt_id=new_attempt, site_uid='123456')
    service.mark_unknown(PRINCIPAL, job)
    assert db.get(job)['result'][STATE]['private_upload_selections'] == 2
    with pytest.raises(HubError, match='SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT'):
        service.operator_retry_private_upload(PRINCIPAL, job, 'Owned third choice must stop')
    assert db.get(job)['attempts'] == 3


def test_private_retry_cannot_reuse_old_browser_attempt_uuid(short_video):
    _, _, service, _, _, _ = short_video
    job, attempt = private_unknown(short_video)
    service.operator_retry_private_upload(PRINCIPAL, job, 'Owned operator reference')
    with pytest.raises(HubError, match='SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT'):
        service.start_upload(PRINCIPAL, job, attempt_id=attempt, site_uid='123456')


@pytest.mark.parametrize('evidence', ['approval', 'bv', 'result_observation', 'historical_reserve', 'historical_submitting', 'publication_rejected', 'publication_unknown'])
def test_private_retry_never_reopens_any_public_submission_evidence(short_video, evidence):
    _, db, service, _, _, _ = short_video
    job, _ = private_unknown(short_video); current = db.get(job); result = current['result']
    if evidence == 'approval': result[STATE]['public_submission_approved'] = True
    elif evidence == 'bv':
        with db.transaction() as conn: conn.execute('UPDATE jobs SET external_publish_id=? WHERE job_id=?', (BV, job))
    elif evidence == 'result_observation': result[STATE]['result_observation'] = {'external_id': BV}
    elif evidence.startswith('historical_'):
        db.audit(PRINCIPAL, 'short_video_reserve_submit' if evidence.endswith('reserve') else 'short_video_checkpoint', job,
                 'one_artifact_one_click_reserved' if evidence.endswith('reserve') else 'submitting')
    else:
        with db.transaction() as conn:
            conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         ('e'*64, job, 'bilibili', 'youtube', current['source_id'], 'site_uid:123456', KIND,
                          evidence.removeprefix('publication_'), None, None, 1))
    with db.transaction() as conn: conn.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(result), job))
    with pytest.raises(HubError): service.operator_retry_private_upload(PRINCIPAL, job, 'Owned operator reference')
    assert db.get(job)['result'][STATE]['state'] == 'upload_unknown'


@pytest.mark.parametrize('change', ['source_file', 'policy', 'missing_reference', 'wrong_principal'])
def test_private_retry_rechecks_actual_permissions_source_and_operator(short_video, change):
    _, db, service, _, _, policy_file = short_video
    job, _ = private_unknown(short_video)
    if change == 'source_file': Path(db.get(job)['payload']['file']).write_bytes(b'changed owned fixture')
    elif change == 'policy': policy_file.unlink()
    with pytest.raises(HubError):
        service.operator_retry_private_upload('reader' if change == 'wrong_principal' else PRINCIPAL, job,
                                             '' if change == 'missing_reference' else 'Owned actual operator reference')
    assert db.get(job)['result'][STATE]['state'] == 'upload_unknown'


def category_amendment_uploaded(f, *, second_attempt=False):
    _, db, service, _, metadata, _ = f
    metadata.pop('category_id'); metadata['category_label'] = '虚拟UP主'
    if not second_attempt:
        return upload(f)
    job, _ = private_unknown(f)
    service.operator_retry_private_upload(PRINCIPAL, job, 'Owned bounded retry, first private blob remains unknown')
    attempt = str(uuid.uuid4()); binding = db.get(job)['payload']
    service.start_upload(PRINCIPAL, job, attempt_id=attempt, site_uid='123456')
    service.record_upload(PRINCIPAL, job, {'version': 'short-video-browser-upload-v1', 'attempt_id': attempt,
        'site_uid': '123456', 'artifact_sha256': binding['artifact_sha256'], 'file_bytes': binding['file_bytes'],
        'upload_reference_sha256': 'b'*64, 'same_attempt_observed': True, 'server_file_hash_verified': False})
    return job, attempt, binding


def amend_category(service, job, metadata_hash, **changes):
    arguments = {'expected_metadata_sha256': metadata_hash,
                 'actual_operator_reference': 'Owned native menu contains Entertainment, no virtual creator category',
                 'actual_owner_source_confirmation': 'Owned synthetic owner statement: original source is black',
                 'source_confirmation_reference': 'Owned synthetic owner statement fixture; not public approval'}
    arguments.update(changes)
    return service.operator_amend_category_v1(PRINCIPAL, job, **arguments)


def test_category_revision_preserves_second_attempt_unknown_history_files_and_unapproved_state(short_video):
    s, db, service, _, _, _ = short_video
    job, attempt, original = category_amendment_uploaded(short_video, second_attempt=True)
    folder = s.root/'media'/job/'short-video-attempts'/attempt; folder.mkdir(parents=True)
    old_files = {'metadata.json': encode(original['metadata']), 'spec.json': encode({'metadata_sha256': original['metadata_sha256']}),
                 'upload-observation.json': encode(db.get(job)['result'][STATE]['upload_observation'])}
    for name, value in old_files.items(): (folder/name).write_text(value, encoding='utf-8')
    with db.transaction() as conn: conn.execute('UPDATE jobs SET attempts=3 WHERE job_id=?', (job,))
    before = db.get(job); checkpoint = deepcopy(before['result'][STATE])
    response = amend_category(service, job, original['metadata_sha256'])
    current, revised, state = service.snapshot(job)
    assert current['attempts'] == 3 and current['state'] == before['state'] == 'upload_pending'
    assert state['state'] == checkpoint['state'] == 'uploaded'
    assert state['attempt_id'] == attempt and state['private_upload_selections'] == 2
    assert state['private_upload_history'] == checkpoint['private_upload_history']
    assert state['upload_observation'] == checkpoint['upload_observation']
    assert state['key'] == checkpoint['key'] and state['orphan_private_blob_unknown'] is True
    assert revised == {**original, 'metadata': {**original['metadata'], 'category_label': '娱乐'},
                       'metadata_sha256': digest({**original['metadata'], 'category_label': '娱乐'})}
    history = state['metadata_revision_history'][0]
    assert history['previous_binding'] == original and history['previous_binding_sha256'] == digest(original)
    assert history['changed_fields'] == ['category_label'] and history['attempt_id'] == attempt
    assert response['binding_sha256'] == digest(revised) and response['private_upload_selections'] == 2
    assert response['public_submit'] is False and response['remote_metadata_synchronized'] is False
    assert state['public_submission_approved'] is False and state['human_review'] is False
    confirmation = state['source_characteristic_confirmation']
    assert confirmation['actual_owner_response'] == 'Owned synthetic owner statement: original source is black'
    assert confirmation['creator_original_bytes_verified'] is False and confirmation['human_review'] is False
    assert confirmation['public_submission_authorized'] is False and 'owner_acceptance' not in state
    assert not db.query('SELECT * FROM publications') and not db.query('SELECT * FROM publication_reviews')
    assert all((folder/name).read_text(encoding='utf-8') == value for name,value in old_files.items())
    assert len(db.query("SELECT * FROM audit WHERE target=? AND action='short_video_amend_category_v1'", (job,))) == 1
    with pytest.raises(HubError, match='SHORT_VIDEO_EXPLICIT_ARTIFACT_ACCEPTANCE_REQUIRED'):
        service.reserve_submit(PRINCIPAL, job, attempt_id=attempt, site_uid='123456', upload_reference_sha256='b'*64)
    with pytest.raises(HubError, match='SHORT_VIDEO_EXPLICIT_ARTIFACT_ACCEPTANCE_REQUIRED'):
        service.approve(PRINCIPAL, job, binding_sha256=digest(original), actual_owner_response='Owned stale binding acceptance',
                        approval_reference='Owned stale fixture reference')
    with pytest.raises(HubError): service.start_upload(PRINCIPAL, job, attempt_id=str(uuid.uuid4()), site_uid='123456')
    with pytest.raises(HubError): amend_category(service, job, revised['metadata_sha256'])
    assert db.get(job)['result'][STATE]['metadata_revision_history'] == [history]


@pytest.mark.parametrize('arguments', [
    {'expected_metadata_sha256': '0'*64}, {'expected_metadata_sha256': 'invalid'},
    {'actual_operator_reference': ''}, {'actual_owner_source_confirmation': ''},
    {'source_confirmation_reference': ''}])
def test_category_revision_requires_exact_existing_hash_and_real_operator_statement_references(short_video, arguments):
    _, db, service, _, _, _ = short_video
    job, _, binding = category_amendment_uploaded(short_video); before = db.get(job)
    with pytest.raises(HubError): amend_category(service, job, binding['metadata_sha256'], **arguments)
    assert db.get(job)['payload'] == before['payload'] and db.get(job)['result'] == before['result']


@pytest.mark.parametrize('evidence', ['approval', 'owner_acceptance', 'bv', 'result_observation',
    'historical_reserve', 'historical_submitting', 'publication_rejected', 'publication_unknown'])
def test_category_revision_cannot_erase_any_public_submission_evidence(short_video, evidence):
    _, db, service, _, _, _ = short_video
    job, _, binding = category_amendment_uploaded(short_video); current = db.get(job); value = current['result']
    if evidence == 'approval': value[STATE]['public_submission_approved'] = True
    elif evidence == 'owner_acceptance': value[STATE]['owner_acceptance'] = {}
    elif evidence == 'result_observation': value[STATE]['result_observation'] = {'external_id': BV}
    elif evidence == 'bv':
        with db.transaction() as conn: conn.execute('UPDATE jobs SET external_publish_id=? WHERE job_id=?', (BV, job))
    elif evidence.startswith('historical_'):
        db.audit(PRINCIPAL, 'short_video_reserve_submit' if evidence.endswith('reserve') else 'short_video_checkpoint', job,
                 'one_artifact_one_click_reserved' if evidence.endswith('reserve') else 'submitting')
    else:
        with db.transaction() as conn:
            conn.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         ('e'*64, job, 'bilibili', 'youtube', current['source_id'], 'site_uid:123456', KIND,
                          evidence.removeprefix('publication_'), None, None, 1))
    with db.transaction() as conn: conn.execute('UPDATE jobs SET result=? WHERE job_id=?', (encode(value), job))
    before = db.get(job)
    with pytest.raises(HubError): amend_category(service, job, binding['metadata_sha256'])
    assert db.get(job)['payload'] == before['payload'] and db.get(job)['result'] == before['result']


@pytest.mark.parametrize('change', ['prepared', 'upload_unknown', 'other_label', 'category_id',
    'tampered_receipt', 'receipt_fields', 'receipt_reference', 'receipt_not_dict',
    'changed_file', 'revoked_policy', 'wrong_principal', 'count_mismatch'])
def test_category_revision_is_narrow_and_rechecks_source_policy_upload_and_operator(short_video, change):
    _, db, service, _, _, policy_file = short_video
    job, _, binding = category_amendment_uploaded(short_video); current = db.get(job)
    payload, value = deepcopy(current['payload']), deepcopy(current['result'])
    if change in ('prepared', 'upload_unknown'): value[STATE]['state'] = change
    elif change == 'other_label': payload['metadata']['category_label'] = '音乐'; payload['metadata_sha256'] = digest(payload['metadata'])
    elif change == 'category_id':
        payload['metadata'].pop('category_label'); payload['metadata']['category_id'] = 65; payload['metadata_sha256'] = digest(payload['metadata'])
    elif change == 'tampered_receipt': value[STATE]['upload_observation']['artifact_sha256'] = '0'*64
    elif change == 'receipt_fields': value[STATE]['upload_observation']['invented_remote_id'] = 'unverified'
    elif change == 'receipt_reference': value[STATE]['upload_observation']['upload_reference_sha256'] = 'not-a-local-digest'
    elif change == 'receipt_not_dict': value[STATE]['upload_observation'] = None
    elif change == 'count_mismatch': value[STATE]['private_upload_selections'] = 2
    elif change == 'changed_file': Path(payload['file']).write_bytes(b'Changed owned synthetic source')
    elif change == 'revoked_policy': policy_file.unlink()
    with db.transaction() as conn: conn.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?', (encode(payload), encode(value), job))
    before = db.get(job)
    with pytest.raises(HubError):
        if change == 'wrong_principal':
            service.operator_amend_category_v1('reader', job, expected_metadata_sha256=payload['metadata_sha256'],
                actual_operator_reference='Owned operator ref', actual_owner_source_confirmation='Owned fixture black source',
                source_confirmation_reference='Owned fixture statement')
        else: amend_category(service, job, payload['metadata_sha256'])
    assert db.get(job)['payload'] == before['payload'] and db.get(job)['result'] == before['result']
