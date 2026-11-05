"""Real local preview rendering, unsigned drafts and protocol rejections. No human acceptance."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from adapters.compositor import Compositor,compositor_input
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.evidence import machine_evidence,sha256_file
from services.hub.review_protocol import digest,load_reviewers,read_review_json
from services.hub.review_signer import sign
from services.hub.tweet_reviews import TweetReviews
from services.hub.video_reviews import VideoReviews

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def draft(runtime):
    settings,store=runtime
    settings.config['sources']=[{'x_handle':'fixture_only'}]
    settings.config['target_bilibili_site_uid']='98765'
    settings.config['target_bilibili_account_name']='Synthetic target; no platform account'
    source={'post':{'id':'1000000000000000012','text':'今日20:00\nよろしく！',
        'created_at':'2026-10-30T12:00:00Z','referenced_tweets':[]},
        'author':{'name':'Owned synthetic fixture','username':'fixture_only'},
        'url':'https://x.com/fixture_only/status/1000000000000000012',
        'source_api':'manual-input','source_media_count':0,'references_observed':True}
    translation='今天20:00\n请多关照！'
    ident=store.enqueue('x',source['post']['id'],'tweet_process','tweet',source,settings.config['policy_version'])
    folder=settings.root/'media'/ident;folder.mkdir(parents=True)
    archive=folder/'source.json';archive.write_text(encode(source),'utf-8')
    item=compositor_input(source,translation);item['testFixture']=True
    render=Compositor(ROOT).render(item,folder)
    evidence=machine_evidence(source,translation,{'passed':True,'issues':[],'synthetic_only':True})
    (folder/'machine-evidence-v1.json').write_text(encode(evidence),'utf-8')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='awaiting_review',stage='rendered_awaiting_human',result=? WHERE job_id=?",
            (encode({'source_sha256':sha256_file(archive),'render':render,'evidence':evidence}),ident))
    service=TweetReviews(settings,store)
    prepared=service.prepare(ident);packet=read_review_json(prepared['file'])
    return settings,store,service,prepared,packet


def key_for(settings,scope='tweet'):
    key=Ed25519PrivateKey.generate()
    public=base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode()
    file=settings.root/'config/reviewers.toml';file.parent.mkdir(exist_ok=True)
    file.write_text(f'''[[reviewers]]
id = "synthetic-reviewer"
public_key = "{public}"
enabled = true
scopes = ["{scope}"]
''','utf-8')
    return key


def denial(packet,key,decision='reject'):
    value=deepcopy(packet)
    value['attestation'].update(reviewer_id='synthetic-reviewer',decision=decision,
        notes='Synthetic protocol denial only; no human acceptance or platform publication.')
    assert not any(value['attestation']['checks'].values())
    value['signature']=base64.urlsafe_b64encode(key.sign(encode(value['attestation']).encode())).decode()
    return value


def test_unsigned_draft_keeps_legacy_checks_and_cannot_authorize_publication(draft):
    settings,store,service,prepared,packet=draft
    assert packet['attestation']['decision']=='pending' and packet['signature']==''
    assert not any(packet['attestation']['checks'].values())
    assert prepared['approval_blockers']==['TWEET_PUBLICATION_RIGHTS_REQUIRED','TWEET_FINAL_RENDER_REQUIRED']
    assert packet['manifest']['render_mode']=='preview'
    assert packet['manifest']['target']['identity_kind']=='website_uid_not_api_openid'
    assert service.readiness()['authorized_signers']==0
    assert service.readiness()['publishing_enabled_by_review'] is False
    item=read_review_json(settings.root/packet['manifest']['input']['path'])
    assert not any(item['post']['review'].values()) and not item['post']['sourceScreenshot']['reviewed']
    assert not store.query('SELECT * FROM tweet_review_bindings')
    assert not store.query('SELECT * FROM publication_reviews')
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


def test_preparing_again_preserves_edited_unsigned_packet(draft):
    settings,store,service,prepared,packet=draft
    packet['attestation']['notes']='Human has not reviewed yet.'
    Path(prepared['file']).write_text(encode(packet),'utf-8')
    again=service.prepare(packet['manifest']['job_id'])
    assert again['request_id']==prepared['request_id']
    assert read_review_json(prepared['file'])['attestation']['notes']=='Human has not reviewed yet.'
    assert len(store.query('SELECT * FROM tweet_review_requests'))==1


def test_signed_rejection_survives_reopen_without_approval_or_enqueue(draft):
    settings,store,service,prepared,packet=draft
    value=denial(packet,key_for(settings))
    result=service.submit(value)
    restored=TweetReviews(settings,Store(settings.db_path))
    assert restored.submit(value)==result
    assert result['decision']=='rejected' and not result['published'] and not result['job_queued']
    assert store.get(packet['manifest']['job_id'])['error_code']=='HUMAN_TWEET_REVIEW_REJECTED'
    assert len(store.query('SELECT * FROM tweet_review_decisions'))==1
    assert not store.query('SELECT * FROM tweet_review_bindings')
    assert not store.query('SELECT * FROM publication_reviews') and not store.query('SELECT * FROM publications')


@pytest.mark.parametrize('change',['signature','manifest','unknown_signer','disabled_signer','video_scope'])
def test_signature_identity_scope_and_manifest_fail_closed(draft,change):
    settings,store,service,prepared,packet=draft
    key=key_for(settings,'video' if change=='video_scope' else 'tweet')
    value=denial(packet,key)
    if change=='signature':value['signature']=base64.urlsafe_b64encode(b'x'*64).decode()
    if change=='manifest':value['manifest']['target']['site_uid']='11111'
    if change=='unknown_signer':value['attestation']['reviewer_id']='unknown'
    if change=='disabled_signer':
        file=settings.root/'config/reviewers.toml';file.write_text(file.read_text().replace('enabled = true','enabled = false'))
    with pytest.raises(HubError):service.submit(value)
    assert service.request(prepared['request_id'])['decision']=='pending'
    assert not store.query('SELECT * FROM tweet_review_decisions')
    assert not store.query('SELECT * FROM publication_reviews')


def test_incomplete_approval_signature_does_not_replace_real_human_checks(draft):
    settings,store,service,prepared,packet=draft
    with pytest.raises(HubError,match='HUMAN_REVIEW_CHECKS_INCOMPLETE'):
        service.submit(denial(packet,key_for(settings),decision='approve'))
    assert not store.query('SELECT * FROM tweet_review_bindings')
    assert not store.query('SELECT * FROM publication_reviews')


def test_expired_request_is_replaced_with_new_nonce(draft):
    settings,store,service,prepared,packet=draft
    key=key_for(settings)
    with store.transaction() as db:db.execute('UPDATE tweet_review_requests SET expires_at=0')
    with pytest.raises(HubError,match='HUMAN_REVIEW_REQUEST_EXPIRED'):service.submit(denial(packet,key))
    assert service.request(prepared['request_id'])['decision']=='expired'
    assert service.prepare(packet['manifest']['job_id'])['request_id']!=prepared['request_id']


def test_pending_request_can_be_revoked_without_content_acceptance(draft):
    settings,store,service,prepared,packet=draft
    assert service.submit(denial(packet,key_for(settings),decision='revoke'))['decision']=='revoked'
    assert not store.query('SELECT * FROM tweet_review_bindings')
    assert not store.query('SELECT * FROM publications')


@pytest.mark.parametrize('changed,code',[('source','HUMAN_REVIEW_INPUT_CHANGED'),
    ('input','HUMAN_REVIEW_INPUT_CHANGED'),('png','HUMAN_REVIEW_RENDER_MISMATCH')])
def test_actual_source_input_and_png_tampering_are_detected(draft,changed,code):
    settings,store,service,prepared,packet=draft
    item={'source':'source','input':'input','png':'artifact'}[changed]
    file=settings.root/packet['manifest'][item]['path']
    file.write_bytes(file.read_bytes()+b'\n')
    with pytest.raises(HubError,match=code):service.prepare(packet['manifest']['job_id'])


def test_quality_and_target_changes_expire_old_pending_request(draft):
    settings,store,service,prepared,packet=draft
    file=settings.root/packet['manifest']['translation_evidence']['path']
    value=read_review_json(file);value['machine_checks_passed']=False;file.write_text(encode(value),'utf-8')
    settings.config['target_bilibili_site_uid']='98766'
    result=service.prepare(packet['manifest']['job_id'])
    assert result['request_id']!=prepared['request_id']
    assert 'TRANSLATION_CHECK_FAILED' in result['approval_blockers']
    assert service.request(prepared['request_id'])['decision']=='expired'


def test_real_publish_mode_refuses_the_unreviewed_input_without_fabricating_flags(draft):
    settings,store,service,prepared,packet=draft
    file=settings.root/packet['manifest']['input']['path']
    original=file.read_bytes()
    with pytest.raises(HubError,match='COMPOSITOR_PUBLISH_BLOCKED'):
        service.prepare(packet['manifest']['job_id'],file)
    assert file.read_bytes()==original
    assert not list((file.parent/'human-review-candidates').rglob('*.publish.png'))
    records=[read_review_json(p)['report'] for p in (file.parent/'human-review-candidates').rglob('hub-render-result.json')]
    assert records and {'SOURCE_UNREVIEWED','TRANSLATION_UNREVIEWED','SCREENSHOT_MISSING'}<=set(i['code'] for i in records[0]['issues'])
    assert records[0]['png'] is None and not records[0]['publishable']
    assert len(store.query('SELECT * FROM tweet_review_requests'))==1
    assert not store.query('SELECT * FROM publications')


def test_outside_job_candidate_is_refused_before_render(draft):
    settings,store,service,prepared,packet=draft
    file=settings.root/'outside.json';file.write_text('{}')
    with pytest.raises(HubError,match='HUMAN_REVIEW_ARTIFACT_PATH_INVALID'):
        service.prepare(packet['manifest']['job_id'],file)


def test_corrupt_candidate_shape_has_bounded_error(draft):
    settings,store,service,prepared,packet=draft
    file=settings.root/'media'/packet['manifest']['job_id']/'bad-candidate.json';file.write_text('{"post": null}')
    with pytest.raises(HubError,match='HUMAN_REVIEW_INPUT_INVALID'):
        service.prepare(packet['manifest']['job_id'],file)


def test_scope_separation_preserves_video_review_rules(runtime):
    settings,store=runtime
    key_for(settings,'tweet')
    assert len(load_reviewers(settings,'tweet'))==1 and not VideoReviews(settings,store).reviewers()
    key_for(settings,'video')
    assert len(VideoReviews(settings,store).reviewers())==1 and not load_reviewers(settings,'tweet')
    file=settings.root/'config/reviewers.toml'
    file.write_text(file.read_text().replace('["video"]','["video", "tweet"]'))
    assert len(VideoReviews(settings,store).reviewers())==len(load_reviewers(settings,'tweet'))==1


def test_interactive_local_signer_handles_only_a_synthetic_rejection(draft,monkeypatch):
    settings,store,service,prepared,packet=draft
    key=key_for(settings)
    packet['attestation'].update(reviewer_id='synthetic-reviewer',decision='reject')
    Path(prepared['file']).write_text(encode(packet),'utf-8')
    monkeypatch.setattr('services.hub.review_signer.interactive',lambda:None)
    monkeypatch.setattr('services.hub.review_signer.load_key',lambda _:key)
    monkeypatch.setattr('builtins.input',lambda _: 'REJECT '+prepared['request_id'])
    output=Path(prepared['file']).with_suffix('.rejected.json')
    sign(prepared['file'],'unused-test-key',output)
    assert service.submit_file(output)['decision']=='rejected'
    assert not store.query('SELECT * FROM tweet_review_bindings')
    packet['attestation']['version']=[]
    Path(prepared['file']).write_text(encode(packet),'utf-8')
    with pytest.raises(HubError,match='HUMAN_REVIEW_ATTESTATION_INVALID'):
        sign(prepared['file'],'unused-test-key',output.with_suffix('.invalid.json'))


def test_missing_job_and_request_are_explicit_errors(runtime):
    settings,store=runtime;service=TweetReviews(settings,store)
    with pytest.raises(HubError,match='TWEET_REVIEWABLE_CHECKPOINT_REQUIRED'):service.verify_approval(None)
    with pytest.raises(HubError,match='HUMAN_REVIEW_REQUEST_ID_INVALID'):service.request('../bad')


def test_video_version_cannot_be_replayed_as_tweet_attestation(draft):
    settings,store,service,prepared,packet=draft
    key=key_for(settings)
    value=denial(packet,key)
    value['attestation']['version']='human-video-review-v1'
    value['signature']=base64.urlsafe_b64encode(key.sign(encode(value['attestation']).encode())).decode()
    with pytest.raises(HubError,match='HUMAN_REVIEW_ATTESTATION_INVALID'):service.submit(value)
    assert not store.query('SELECT * FROM tweet_review_decisions')


def test_synthetic_rights_are_bound_to_exact_source_actions_and_target(draft):
    settings,store,service,prepared,packet=draft
    folder=settings.root/'config';folder.mkdir(exist_ok=True)
    file=folder/'tweet-rights.toml'
    grant=f'''[[grants]]
post_id = "1000000000000000012"
x_handle = "fixture_only"
source_sha256 = "{packet['manifest']['source']['sha256']}"
actions = ["publish_translation", "publish_image"]
evidence = "owned-synthetic-policy-fixture-not-creator-permission"
authorized_by = "synthetic-fixture-only"
target_site_uid = "98765"
'''
    file.write_text(grant,'utf-8')
    result=service.prepare(packet['manifest']['job_id'])
    manifest=read_review_json(result['file'])['manifest']
    assert manifest['rights']['valid'] and result['approval_blockers']==['TWEET_FINAL_RENDER_REQUIRED']
    assert result['request_id']!=prepared['request_id']
    for invalid in (grant.replace('"publish_image"','"unapproved_action"'),grant+grant,
                    grant.replace('target_site_uid = "98765"','target_site_uid = "11111"'),
                    grant.replace(packet['manifest']['source']['sha256'],'0'*64)):
        file.write_text(invalid,'utf-8')
        assert service.snapshot(store.get(packet['manifest']['job_id']))['rights']['valid'] is False
    assert not store.query('SELECT * FROM tweet_review_bindings') and not store.query('SELECT * FROM publication_reviews')


def test_original_image_bytes_are_revalidated_against_real_renderer_assets(draft):
    import struct,zlib
    settings,store,service,prepared,packet=draft
    folder=settings.root/'media'/packet['manifest']['job_id']
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))
    # An owned 16x16 solid-color avatar, no real person's image or source screenshot.
    avatar=folder/'owned-synthetic-avatar.png'
    avatar.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',16,16,8,6,0,0,0))+
        chunk(b'IDAT',zlib.compress((b'\x00'+b'\x88\x44\x22\xff'*16)*16))+chunk(b'IEND',b''))
    item=read_review_json(folder/'compositor-input.json');item['post']['author']['avatar']=str(avatar)
    rendered=Compositor(ROOT).render(item,folder)
    job=store.get(packet['manifest']['job_id']);result={**job['result'],'render':rendered}
    with store.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(result),job['job_id']))
    service.prepare(job['job_id'])
    avatar.write_bytes(avatar.read_bytes()+b'tampered-original-asset')
    with pytest.raises(HubError,match='HUMAN_REVIEW_ASSET_CHANGED'):service.prepare(job['job_id'])
    assert not store.query('SELECT * FROM publication_reviews')
