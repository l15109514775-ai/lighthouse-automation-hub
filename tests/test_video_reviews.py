"""Synthetic unsigned drafts and signed REJECTIONS; never fabricate human acceptance."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.review_signer import create_key,sign
from services.hub.video_reviews import CHECKS,VideoReviews,digest,read_review_json
from services.hub.youtube_monitor import YouTubeMonitor


@pytest.fixture
def review_draft(runtime):
    settings,store=runtime
    settings.config.update(authorized_bilibili_accounts=['synthetic-openid'],target_bilibili_account_name='synthetic-review-only')
    channel=settings.config['sources'][0]['youtube_channel_id']
    raw={'id':'abcdefghijk','snippet':{'channelId':channel,'title':'Synthetic, not a real broadcast',
        'publishedAt':'2026-10-30T00:00:00Z'},'status':{'privacyStatus':'public'},
        'contentDetails':{'duration':'PT2S'},'liveStreamingDetails':{
        'actualStartTime':'2026-10-30T01:00:00Z','actualEndTime':'2026-10-30T01:00:02Z'}}
    YouTubeMonitor(settings,store,None).observe_video(raw,channel)
    ident=store.query("SELECT job_id FROM jobs WHERE job_type='video_process'")[0]['job_id']
    folder=settings.root/'media'/ident
    folder.mkdir(parents=True)
    source=folder/'original.mp4';source.write_bytes(b'Synthetic source bytes, no real content or license.')
    artifact=folder/'processed.mp4';artifact.write_bytes(b'Synthetic output bytes; probe is explicitly injected.')
    source_hash=sha256_file(source)
    url='https://www.youtube.com/watch?v=abcdefghijk'
    config=settings.root/'config';config.mkdir(exist_ok=True)
    rights=f'''[[grants]]
video_id = "abcdefghijk"
actions = ["download", "process", "publish"]
evidence = "synthetic-unapproved-protocol-reference"
authorized_by = "synthetic-policy-fixture"
source_sha256 = "{source_hash}"
source_url = "https://example.invalid/synthetic-only.mp4?private-source-token=synthetic-not-exported"
max_bytes = 1024
[grants.bilibili]
title = "Synthetic unsigned review"
tid = 1
tag = "synthetic-only"
desc = "Synthetic source: {url}"
copyright = 2
source = "{url}"
'''
    (config/'rights.toml').write_text(rights,'utf-8')
    result={'source_asset':{'file':str(source),'sha256':source_hash},'source_sha256':source_hash,
        'processed':{'file':str(artifact),'sha256':sha256_file(artifact),'duration':2.0},
        'rights_evidence':'synthetic-unapproved-protocol-reference'}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='awaiting_review',stage='video_ready_for_review',result=? WHERE job_id=?",(encode(result),ident))
        db.execute('INSERT INTO bilibili_accounts VALUES(?,?,?,?,?)',('synthetic-openid','synthetic-app',
            'synthetic-review-only',encode(['USER_INFO','ARC_BASE']),time.time()))
    service=VideoReviews(settings,store,probe=lambda _:2.0)
    prepared=service.prepare(ident,'synthetic-openid')
    packet=read_review_json(prepared['file'])
    return settings,store,service,prepared,packet


def protocol_signer(settings):
    key=Ed25519PrivateKey.generate()
    public=base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode()
    (settings.root/'config/reviewers.toml').write_text(f'''[[reviewers]]
id = "synthetic-reviewer"
public_key = "{public}"
enabled = true
scopes = ["video"]
''','utf-8')
    return key


def rejection(packet,key,decision='reject'):
    value=deepcopy(packet)
    value['attestation'].update(reviewer_id='synthetic-reviewer',decision=decision,notes='Synthetic protocol denial; no human acceptance.')
    assert not any(value['attestation']['checks'].values())
    value['signature']=base64.urlsafe_b64encode(key.sign(encode(value['attestation']).encode())).decode()
    return value


def test_default_draft_is_unsigned_never_approved_or_queued(review_draft):
    settings,store,service,prepared,packet=review_draft
    assert packet['signature']=='' and packet['attestation']['decision']=='pending'
    assert not any(packet['attestation']['checks'].values())
    assert service.readiness()['authorized_signers']==0
    assert not service.readiness()['ready_for_signed_review']
    assert 'private-source-token' not in encode(packet)
    assert store.get(packet['manifest']['job_id'])['state']=='awaiting_review'
    assert not store.query('SELECT * FROM publication_reviews')
    assert not store.query('SELECT * FROM publications')


def test_prepare_deduplicates_and_preserves_operators_edited_draft(review_draft):
    settings,store,service,prepared,packet=review_draft
    edited=Path(prepared['file']);packet['attestation']['notes']='Operator has not decided.'
    edited.write_text(encode(packet),'utf-8')
    again=service.prepare(packet['manifest']['job_id'],'synthetic-openid')
    assert again['request_id']==prepared['request_id']
    assert read_review_json(edited)['attestation']['notes']=='Operator has not decided.'
    assert store.query('SELECT COUNT(*) n FROM video_review_requests')[0]['n']==1


def test_signed_rejection_is_durable_and_idempotent_without_human_approval(review_draft):
    settings,store,service,prepared,packet=review_draft
    key=protocol_signer(settings)
    denied=rejection(packet,key)
    result=service.submit(denied)
    assert result['decision']=='rejected' and not result['published'] and not result['job_queued']
    restored=VideoReviews(settings,Store(settings.db_path),probe=lambda _:2)
    assert restored.submit(denied)==result
    assert store.get(packet['manifest']['job_id'])['error_code']=='HUMAN_VIDEO_REVIEW_REJECTED'
    assert store.query('SELECT COUNT(*) n FROM audit WHERE outcome=?',('rejected',))[0]['n']==1
    assert store.query('SELECT COUNT(*) n FROM video_review_decisions')[0]['n']==1
    assert not store.query('SELECT * FROM publication_reviews')
    assert not store.query('SELECT * FROM video_review_bindings')


@pytest.mark.parametrize('change', ['signature','manifest','unknown_signer','disabled_signer'])
def test_signature_manifest_and_signer_tampering_are_rejected(review_draft,change):
    settings,store,service,prepared,packet=review_draft
    key=protocol_signer(settings);denied=rejection(packet,key)
    if change=='signature': denied['signature']=base64.urlsafe_b64encode(b'x'*64).decode()
    elif change=='manifest': denied['manifest']['submission']['title']='tampered'
    elif change=='unknown_signer': denied['attestation']['reviewer_id']='not-authorized'
    else:
        file=settings.root/'config/reviewers.toml'
        file.write_text(file.read_text().replace('enabled = true','enabled = false'))
    with pytest.raises(HubError): service.submit(denied)
    assert service.request(prepared['request_id'])['decision']=='pending'
    assert not store.query('SELECT * FROM publication_reviews')


def test_unchecked_approval_is_blocked_even_with_valid_protocol_signature(review_draft):
    settings,store,service,prepared,packet=review_draft
    # Checks remain false; this deliberately tests an INCOMPLETE approval, not acceptance.
    key=protocol_signer(settings)
    value=rejection(packet,key,decision='approve')
    with pytest.raises(HubError,match='CHECKS_INCOMPLETE'): service.submit(value)
    assert not store.query('SELECT * FROM publication_reviews')
    assert store.get(packet['manifest']['job_id'])['state']=='awaiting_review'


def test_expired_nonce_is_not_replayed_and_replacement_uses_new_id(review_draft):
    settings,store,service,prepared,packet=review_draft
    key=protocol_signer(settings)
    with store.transaction() as db:
        db.execute('UPDATE video_review_requests SET expires_at=0')
    with pytest.raises(HubError,match='REQUEST_EXPIRED'): service.submit(rejection(packet,key))
    assert service.request(prepared['request_id'])['decision']=='expired'
    replacement=service.prepare(packet['manifest']['job_id'],'synthetic-openid')
    assert replacement['request_id']!=prepared['request_id']


def test_pending_request_can_be_cancelled_without_authorizing_content(review_draft):
    settings,store,service,prepared,packet=review_draft
    result=service.submit(rejection(packet,protocol_signer(settings),decision='revoke'))
    assert result['decision']=='revoked' and not result['job_queued']
    assert not store.query('SELECT * FROM publication_reviews')


@pytest.mark.parametrize('change,code',[('source','SOURCE_HASH_MISMATCH'),('artifact','ARTIFACT_HASH_MISMATCH'),
    ('rights','VIDEO_PUBLICATION_RIGHTS_REQUIRED'),('channel','SOURCE_IDENTITY_INVALID')])
def test_actual_files_rights_and_source_identity_are_revalidated(review_draft,change,code):
    settings,store,service,prepared,packet=review_draft
    job=store.get(packet['manifest']['job_id'])
    if change=='source': Path(job['result']['source_asset']['file']).write_bytes(b'changed source')
    elif change=='artifact': Path(job['result']['processed']['file']).write_bytes(b'changed output')
    elif change=='rights': (settings.root/'config/rights.toml').write_text('grants = []')
    else:
        with store.transaction() as db: db.execute("UPDATE youtube_videos SET state='live'")
    with pytest.raises(HubError,match=code): service.snapshot(job,'synthetic-openid')
    assert not store.query('SELECT * FROM publication_reviews')


def test_changed_submission_invalidates_draft_and_full_grant_is_hashed(review_draft):
    settings,store,service,prepared,packet=review_draft
    file=settings.root/'config/rights.toml'
    file.write_text(file.read_text().replace('Synthetic unsigned review','Changed submission'))
    replacement=service.prepare(packet['manifest']['job_id'],'synthetic-openid')
    changed=read_review_json(replacement['file'])
    assert changed['attestation']['manifest_sha256']!=packet['attestation']['manifest_sha256']
    assert changed['manifest']['rights']['grant_sha256']!=packet['manifest']['rights']['grant_sha256']
    assert service.request(prepared['request_id'])['decision']=='expired'


def test_read_token_cannot_queue_and_maintenance_does_not_override_policy(review_draft,monkeypatch):
    settings,store,service,prepared,packet=review_draft
    monkeypatch.setenv('HUB_READ_TOKEN','synthetic-reader')
    monkeypatch.setenv('HUB_MAINTAIN_TOKEN','synthetic-maintainer')
    with pytest.raises(HubError,match='ACCESS_DENIED'): service.queue(prepared['request_id'],'synthetic-reader')
    with pytest.raises(HubError,match='FIRST_PUBLISH_ACCEPTANCE_REQUIRED'):
        service.queue(prepared['request_id'],'synthetic-maintainer')
    settings.dry_run=False
    settings.config.update(auto_publish=True,first_publish_accepted=True,bilibili_publish_mode='official_video')
    with pytest.raises(HubError,match='SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED'):
        service.queue(prepared['request_id'],'synthetic-maintainer')
    assert store.get(packet['manifest']['job_id'])['state']=='awaiting_review'
    assert not store.query('SELECT * FROM publications')


@pytest.mark.parametrize('data',['{"signature":"one","signature":"two"}','{"x":NaN}','x'*65537])
def test_ambiguous_or_unbounded_review_input_is_rejected(tmp_path,data):
    file=tmp_path/'invalid.json';file.write_text(data,'utf-8')
    with pytest.raises(HubError,match='DOCUMENT_INVALID'): read_review_json(file)


def test_failed_cli_file_submission_is_audited_without_payload_or_signature(review_draft):
    settings,store,service,prepared,packet=review_draft
    packet['attestation']['reviewer_id']='synthetic-reviewer'
    file=Path(prepared['file']);file.write_text(encode(packet),'utf-8')
    with pytest.raises(HubError): service.submit_file(file)
    log=store.query("SELECT * FROM audit WHERE action='submit_video_review'")
    assert len(log)==1 and log[0]['outcome']=='HUMAN_REVIEW_DECISION_REQUIRED'
    assert 'attestation' not in encode(log) and 'synthetic-reviewer' not in encode(log)


def test_signer_rejects_unattended_key_creation_and_unchecked_acceptance(review_draft,monkeypatch):
    import services.hub.review_signer as signer
    settings,store,service,prepared,packet=review_draft
    monkeypatch.setattr(signer.sys.stdin,'isatty',lambda:False)
    with pytest.raises(HubError,match='INTERACTIVE_HUMAN_SIGNING_REQUIRED'):
        create_key(settings.root/'never-created.pem')
    assert not (settings.root/'never-created.pem').exists()
    monkeypatch.setattr(signer.sys.stdin,'isatty',lambda:True)
    monkeypatch.delenv('HUB_ROOT',raising=False)
    packet['attestation'].update(decision='approve',reviewer_id='synthetic-reviewer')
    Path(prepared['file']).write_text(encode(packet),'utf-8')
    with pytest.raises(HubError,match='CHECKS_INCOMPLETE'):
        sign(prepared['file'],settings.root/'never-created.pem',settings.root/'never-signed.json')
    assert not (settings.root/'never-signed.json').exists()


def test_operator_signer_can_sign_only_a_deliberate_synthetic_rejection(review_draft,monkeypatch):
    import services.hub.review_signer as signer
    settings,store,service,prepared,packet=review_draft
    key=protocol_signer(settings)
    key_file=settings.root/'synthetic-encrypted-test-key.pem'
    password='synthetic-test-passphrase-only'
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password.encode())))
    packet['attestation'].update(decision='reject',reviewer_id='synthetic-reviewer')
    Path(prepared['file']).write_text(encode(packet),'utf-8')
    monkeypatch.setattr(signer.sys.stdin,'isatty',lambda:True)
    monkeypatch.delenv('HUB_ROOT',raising=False)
    monkeypatch.setattr(signer.getpass,'getpass',lambda _:password)
    monkeypatch.setattr('builtins.input',lambda _:'REJECT '+prepared['request_id'])
    output=settings.root/'synthetic-signed-rejection.json'
    sign(prepared['file'],key_file,output)
    assert service.submit_file(output)['decision']=='rejected'
    assert not store.query('SELECT * FROM publication_reviews')
