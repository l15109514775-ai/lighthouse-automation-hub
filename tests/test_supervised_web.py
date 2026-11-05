"""Fail-closed tests; no invented positive human review or platform publication."""
import pytest
from services.hub.supervised_web import SupervisedWeb
from services.hub.errors import HubError


def test_reader_cannot_reserve_or_record_and_no_dispatch_occurs(runtime):
    settings, store = runtime
    service = SupervisedWeb(settings, store)
    with pytest.raises(HubError, match='ACCESS_DENIED'):
        service.reserve('reader', 'missing', site_uid='1', artifact_sha256='x', caption_sha256='y')
    with pytest.raises(HubError, match='ACCESS_DENIED'):
        service.record_visible('reader', 'missing', 'missing')
    with pytest.raises(HubError, match='ACCESS_DENIED'):
        service.record_nonacceptance('reader','missing','missing')
    assert not store.query('SELECT * FROM publications')
    assert not store.query('SELECT * FROM api_calls')


def test_unreviewed_job_cannot_be_promoted_to_website_dispatch(runtime):
    settings, store = runtime
    ident = store.enqueue('x', '123', 'tweet_process', 'tweet', {'synthetic': True}, 'human-review-v1')
    with pytest.raises(HubError, match='SUPERVISED_REVIEW_OR_ARTIFACT_INVALID'):
        SupervisedWeb(settings, store).reserve('maintenance', ident, site_uid='1', artifact_sha256='x', caption_sha256='y')
    assert store.get(ident)['state'] == 'pending'
    assert not store.query('SELECT * FROM publications')


def test_official_video_reconciler_and_scheduler_ignore_web_unknown_results(runtime):
    from services.hub.bilibili_service import BilibiliService
    from scheduler.service import DurableScheduler
    settings, store = runtime
    ident=store.enqueue('x','123','tweet_process','tweet',{'synthetic':True},'human-review-v1')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(ident,))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('synthetic-web-key',ident,'bilibili','x','123','site_uid:123','dynamic_website_supervised','unknown',None,None,0))
    result=BilibiliService(settings,store,None).reconcile({})
    assert result['publications_checked']==0 and result['publish_requests_sent']==0
    assert store.get(ident)['state']=='publish_unknown'
    DurableScheduler(settings,store).tick()
    assert not store.query("SELECT * FROM jobs WHERE job_type='bilibili_reconcile'")
    assert not store.query('SELECT * FROM api_calls')
