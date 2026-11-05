"""In-memory website transport only. No signed human rows, live account or upload."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from adapters.bilibili_web_video import BilibiliWebsiteVideo,NAMESPACE
from services.hub.db import Store,encode
from services.hub.errors import HubError,PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.publishing import Publishing
from services.hub.review_protocol import digest


BV='BV0000000000' # Owned fictional identifier, never sent to any real platform.


class OwnedWebsiteAuthorization:
    def __init__(self):self.calls=[];self.revoked=False
    def verify(self,binding,*,boundary):
        self.calls.append((deepcopy(binding),boundary))
        if self.revoked:raise HubError('OWNED_WEBSITE_GRANT_REVOKED')
        assert binding['account_kind']=='website_site_uid_not_official_openid'
        return {'version':'website-video-authorization-v1','binding_sha256':digest(binding),
            'reference':'owned mock website grant; never a human attestation'}


class OwnedWebsite:
    def __init__(self):
        self.uploads=0;self.sends=0;self.lookups=0;self.uid='123456';self.name='Owned synthetic account'
        self.fail_upload=False;self.fail_submit=False;self.lookup_state='completed';self.bad_receipt=False;self.submit_state='awaiting_moderation'
    def accountIdentity(self):return {'site_uid':self.uid,'name':self.name}
    def prepareUpload(self,spec):
        self.uploads+=1
        assert Path(spec['file']).stat().st_size==spec['file_bytes']
        assert sha256_file(Path(spec['file']))==spec['artifact_sha256']
        if self.fail_upload:raise TimeoutError('owned missing upload acknowledgement')
        return {'version':'website-video-upload-receipt-v1','key':spec['key'],
            'artifact_sha256':'0'*64 if self.bad_receipt else spec['artifact_sha256'],'file_bytes':spec['file_bytes'],
            'site_uid':self.uid,'upload_reference':'owned-private-draft-1',
            'evidence_kind':'same_browser_attempt_observation','server_file_hash_verified':False}
    def submit(self,spec):
        self.sends+=1
        assert spec['upload_receipt']['upload_reference_sha256']==hashlib.sha256(b'owned-private-draft-1').hexdigest()
        if self.fail_submit:raise TimeoutError('owned lost final click acknowledgement')
        return {'state':self.submit_state,'external_id':BV,'url':'https://www.bilibili.com/video/'+BV}
    def lookup(self,spec):
        self.lookups+=1;assert spec['external_id']==BV
        result={'state':self.lookup_state,'external_id':BV,'url':'https://www.bilibili.com/video/'+BV}
        if self.lookup_state=='failed':result['error_code']='BILIBILI_MODERATION_REJECTED'
        return result


@pytest.fixture
def website(runtime):
    settings,store=runtime
    settings.dry_run=False
    settings.config.update(web_video_prototype_enabled=True,bilibili_publish_mode='website_video_prototype',
        auto_publish=True,first_publish_accepted=True,target_bilibili_site_uid='123456',
        target_bilibili_account_name='Owned synthetic account')
    ident=store.enqueue('youtube','abcdefghijk','video_process','video',
        {'video_id':'abcdefghijk','url':'https://www.youtube.com/watch?v=abcdefghijk'},'human-review-v1')
    job=store.claim('video',180,4);assert job['job_id']==ident
    folder=settings.root/'media'/ident;folder.mkdir()
    source=folder/'original.mp4';source.write_bytes(b'owned synthetic original, not a creator recording')
    file=folder/'processed.mp4';file.write_bytes(b'owned synthetic prepared media; duration probe is explicitly mocked')
    metadata={'title':'Owned synthetic video','tid':1,'tag':'owned,test','desc':'Owned source https://www.youtube.com/watch?v=abcdefghijk',
        'copyright':2,'source':job['payload']['url'],'no_reprint':1}
    grant={'video_id':'abcdefghijk','actions':['download','process','publish'],
        'source_sha256':sha256_file(source),'evidence':'Owned synthetic per-video permission fixture',
        'authorized_by':'Owned fixture authorizer','bilibili':metadata}
    config=settings.root/'config';config.mkdir()
    # JSON string syntax also quotes these simple TOML values safely.
    content='[[grants]]\n'+'\n'.join(k+'='+json.dumps(v) for k,v in grant.items() if k!='bilibili')
    content+='\n[grants.bilibili]\n'+'\n'.join(k+'='+json.dumps(v) for k,v in metadata.items())+'\n'
    (config/'rights.toml').write_text(content,'utf8')
    artifact={'file':str(file),'sha256':sha256_file(file),'bilibili':metadata}
    store.checkpoint(job,'video_ready_for_review',{'source_asset':{'file':str(source),'sha256':sha256_file(source)},
        'source_sha256':sha256_file(source),'rights_evidence':grant['evidence'],
        'processed':{'file':str(file),'sha256':sha256_file(file)}})
    driver=OwnedWebsite();authority=OwnedWebsiteAuthorization()
    adapter=BilibiliWebsiteVideo(settings,store,job,driver,authorization=authority,probe=lambda _:60)
    key=digest(['bilibili','youtube','abcdefghijk','site_uid:123456','video'])
    return settings,store,job,artifact,driver,authority,adapter,key


def dispatch(fixture,*,callback=lambda:None):
    s,d,j,a,driver,authority,adapter,key=fixture
    # Exercise only the existing internal transport coordinator. submit()'s
    # real signed review check is deliberately not bypassed in production.
    return Publishing(s,d,adapter)._dispatch(j,a,'site_uid:123456','video',revalidate=callback)


@pytest.mark.parametrize('block',['dry_run','mode','switch','authority'])
def test_default_or_missing_website_specific_authorization_stops_before_ui(website,block):
    s,d,j,a,driver,authority,adapter,key=website
    if block=='dry_run':s.dry_run=True
    elif block=='mode':s.config['bilibili_publish_mode']='manual'
    elif block=='switch':s.config['web_video_prototype_enabled']=False
    else:adapter.authorization=None
    with pytest.raises(HubError):adapter.prepare(key,a)
    assert driver.uploads==driver.sends==0 and not d.get(j['job_id'])['result'].get(NAMESPACE)
    assert not d.query('SELECT * FROM publication_reviews')


def test_signed_official_review_is_not_implicitly_a_website_uid_authorization(website):
    _,d,_,a,driver,_,adapter,key=website
    class WrongScope:
        def verify(self,binding,*,boundary):
            return {'version':'human-video-review-v1','binding_sha256':digest(binding),'reference':'openid is not site_uid'}
    adapter.authorization=WrongScope()
    with pytest.raises(HubError,match='WEBSITE_VIDEO_AUTHORIZATION_INVALID'):adapter.prepare(key,a)
    assert driver.uploads==driver.sends==0 and not d.query('SELECT * FROM publication_reviews')


def test_existing_public_submit_gate_still_requires_real_signed_review(website):
    s,d,j,a,driver,_,adapter,_=website
    s.config['authorized_bilibili_accounts']=['site_uid:123456']
    with pytest.raises(HubError,match='PUBLISH_REVIEW_EVIDENCE_REQUIRED'):
        Publishing(s,d,adapter).submit(j,a,'site_uid:123456','video')
    assert driver.uploads==driver.sends==0
    assert not d.query('SELECT * FROM publication_reviews') and not d.query('SELECT * FROM publications')


def test_private_upload_is_persisted_reused_after_db_reopen_and_submission_is_deduplicated(website):
    s,d,j,a,driver,authority,adapter,key=website
    adapter.prepare(key,a)
    checkpoint=d.get(j['job_id'])['result'][NAMESPACE]
    assert checkpoint['state']=='uploaded' and checkpoint['upload_receipt']['server_file_hash_verified'] is False
    assert checkpoint['human_review'] is False and checkpoint['authorization_granted_by_transport'] is False
    assert 'owned-private-draft-1' not in encode(d.get(j['job_id'])['result'])
    adapter=BilibiliWebsiteVideo(s,Store(s.db_path),j,driver,authorization=authority,probe=lambda _:60)
    assert adapter.prepare(key,a)['checkpoint_reused'] and driver.uploads==1
    fixture=(s,d,j,a,driver,authority,adapter,key)
    assert dispatch(fixture)['state']=='awaiting_moderation'
    assert dispatch(fixture)['state']=='completed'
    assert driver.uploads==driver.sends==driver.lookups==1
    assert d.query('SELECT * FROM publications')[0]['external_id']==BV
    assert d.get(j['job_id'])['stage']=='video_ready_for_review'
    assert not d.query('SELECT * FROM publication_reviews')


@pytest.mark.parametrize('mode',['timeout','wrong_hash'])
def test_upload_unknown_or_invalid_receipt_never_reselects_file_after_restart(website,mode):
    s,d,j,a,driver,authority,adapter,key=website
    driver.fail_upload=mode=='timeout';driver.bad_receipt=mode=='wrong_hash'
    with pytest.raises((HubError,TimeoutError)):adapter.prepare(key,a)
    assert d.get(j['job_id'])['result'][NAMESPACE]['state']=='upload_unknown'
    reopened=BilibiliWebsiteVideo(s,Store(s.db_path),j,driver,authorization=authority,probe=lambda _:60)
    with pytest.raises(HubError,match='WEB_VIDEO_UPLOAD_RESULT_UNKNOWN'):reopened.prepare(key,a)
    assert driver.uploads==1 and driver.sends==0


def test_submission_unknown_queries_without_resend_and_never_claims_similar_title_or_supplied_bv(website):
    s,d,j,a,driver,authority,adapter,key=website;driver.fail_submit=True
    with pytest.raises(PublishUnknown):dispatch(website)
    assert d.query('SELECT * FROM publications')[0]['state']=='unknown'
    for _ in range(2):
        with pytest.raises(PublishUnknown):dispatch(website)
    assert adapter.lookup(key,BV)=={'state':'unknown'}
    assert driver.sends==1 and driver.uploads==1 and driver.lookups==0
    assert d.get(j['job_id'])['result'][NAMESPACE]['state']=='submit_unknown'


def test_intermediate_bv_receipt_recovers_lost_final_database_write_without_new_click(website):
    s,d,j,a,driver,authority,adapter,key=website
    adapter.prepare(key,a)
    result=adapter.send_reviewed(key,a,lambda:None)
    # Controlled crash point: the page result and job checkpoint reached disk,
    # but Publishing's final publication row update never happened.
    with d.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (key,j['job_id'],'bilibili','youtube',j['source_id'],'site_uid:123456','video','unknown',None,None,0))
    s.dry_run=True;s.config['web_video_prototype_enabled']=False
    adapter.authorization=None
    (s.root/'config/rights.toml').unlink();Path(a['file']).unlink()
    reopened=BilibiliWebsiteVideo(s,Store(s.db_path),j,driver,authorization=None,probe=lambda _:60)
    looked=Publishing(s,d,reopened).reconcile(key)
    assert looked['state']=='completed' and looked['external_id']==result['external_id']
    assert driver.uploads==driver.sends==driver.lookups==1
    assert d.query('SELECT * FROM publications')[0]['state']=='completed'


def test_known_bv_with_unreadable_status_is_preserved_for_lookup_without_success_claim(website):
    s,d,j,a,driver,authority,adapter,key=website;driver.submit_state='unknown'
    with pytest.raises(PublishUnknown):dispatch(website)
    checkpoint=d.get(j['job_id'])['result'][NAMESPACE]
    assert checkpoint['state']=='submit_unknown' and checkpoint['observed_submission']['external_id']==BV
    assert 'submission_receipt' not in checkpoint
    assert dispatch(website)['state']=='completed'
    assert driver.uploads==driver.sends==driver.lookups==1


def test_moderation_rejection_keeps_bound_bv_and_does_not_submit_again(website):
    s,d,j,a,driver,authority,adapter,key=website
    dispatch(website);driver.lookup_state='failed'
    result=Publishing(s,d,adapter).reconcile(key)
    assert result['state']=='failed' and result['error_code']=='BILIBILI_MODERATION_REJECTED'
    assert d.query('SELECT * FROM publications')[0]['external_id']==BV
    assert driver.uploads==driver.sends==driver.lookups==1


def test_caller_cannot_rebind_recorded_submission_to_another_bv(website):
    _,_,_,_,driver,_,adapter,key=website
    dispatch(website)
    with pytest.raises(HubError,match='WEB_VIDEO_BV_BINDING_MISMATCH'):adapter.lookup(key,'BV1111111111')
    assert driver.sends==1 and driver.lookups==0


def test_lost_lease_after_private_upload_stays_uncertain_for_next_real_claim(website):
    s,d,j,a,driver,authority,adapter,key=website
    original=driver.prepareUpload
    def lose_lease(spec):
        value=original(spec)
        with d.transaction() as db:db.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?',(j['job_id'],))
        return value
    driver.prepareUpload=lose_lease
    with pytest.raises(HubError,match='WEB_VIDEO_PARENT_LEASE_LOST'):adapter.prepare(key,a)
    assert d.get(j['job_id'])['result'][NAMESPACE]['state']=='uploading'
    d.recover('video',4);claimed=d.claim('video',180,4)
    assert claimed['job_id']==j['job_id'] and claimed['lease_owner']!=j['lease_owner']
    reopened=BilibiliWebsiteVideo(s,Store(s.db_path),claimed,driver,authorization=authority,probe=lambda _:60)
    with pytest.raises(HubError,match='WEB_VIDEO_UPLOAD_RESULT_UNKNOWN'):reopened.prepare(key,a)
    assert driver.uploads==1 and driver.sends==0


@pytest.mark.parametrize('change',['rights_missing','source_tamper','file_tamper','metadata','multipart','lease','account'])
def test_per_video_source_integrity_rights_lease_and_target_are_required(website,change):
    s,d,j,a,driver,authority,adapter,key=website
    if change=='rights_missing':(s.root/'config/rights.toml').unlink()
    elif change=='source_tamper':Path(d.get(j['job_id'])['result']['source_asset']['file']).write_bytes(b'changed source')
    elif change=='file_tamper':Path(a['file']).write_bytes(b'changed artifact')
    elif change=='metadata':a=deepcopy(a);a['bilibili']['title']='Changed metadata'
    elif change=='multipart':
        with d.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',
            (encode({**d.get(j['job_id'])['result'],'processed_parts':[a,a]}),j['job_id']))
    elif change=='lease':
        with d.transaction() as db:db.execute('UPDATE jobs SET lease_until=0 WHERE job_id=?',(j['job_id'],))
    else:driver.uid='999999'
    with pytest.raises(HubError):adapter.prepare(key,a)
    assert driver.uploads==driver.sends==0
    assert not d.query('SELECT * FROM publications') and not d.query('SELECT * FROM publication_reviews')


def test_website_grant_is_rechecked_between_private_upload_and_public_submit(website):
    s,d,j,a,driver,authority,adapter,key=website
    adapter.prepare(key,a);authority.revoked=True
    with pytest.raises(HubError,match='OWNED_WEBSITE_GRANT_REVOKED'):dispatch(website)
    assert driver.uploads==1 and driver.sends==0


def test_invalid_key_cannot_create_lock_outside_workspace(website):
    _,_,_,a,driver,_,adapter,_=website
    with pytest.raises(HubError,match='INVALID_PUBLICATION_KEY'):adapter.prepare('../../outside',a)
    assert driver.uploads==driver.sends==0


def test_low_disk_refuses_new_private_upload_without_deleting_existing_material(website,monkeypatch):
    s,d,j,a,driver,_,adapter,key=website
    from types import SimpleNamespace
    monkeypatch.setattr('services.hub.maintenance.shutil.disk_usage',lambda _:SimpleNamespace(free=14_000_000_000))
    with pytest.raises(HubError,match='DISK_LOW'):adapter.prepare(key,a)
    assert Path(a['file']).exists() and driver.uploads==driver.sends==0
    assert d.query("SELECT * FROM events WHERE name='disk.low'")


def test_prior_video_via_other_transport_blocks_upload_without_equating_openid_and_site_uid(website):
    _,d,j,a,driver,_,adapter,key=website
    with d.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        ('b'*64,j['job_id'],'bilibili','youtube',j['source_id'],'owned-official-openid','video','unknown',None,None,0))
    with pytest.raises(HubError,match='WEB_VIDEO_EXISTING_PUBLICATION_REQUIRES_LOOKUP'):adapter.prepare(key,a)
    assert driver.uploads==driver.sends==0
