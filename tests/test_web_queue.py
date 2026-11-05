"""No synthetic positive human review, platform login or send."""
import pytest
from services.hub.web_queue import ReviewedWebQueue
from services.hub.errors import HubError
from services.hub.worker import Worker
from scheduler.service import DurableScheduler


def test_reader_and_unreviewed_source_cannot_queue_publication(runtime):
    settings,store=runtime;settings.config['web_reviewed_queue_enabled']=True
    ident=store.enqueue('x','123','tweet_process','tweet',{'synthetic':True},'human-review-v1')
    service=ReviewedWebQueue(settings,store)
    request={'manifest_sha256':'0'*64,'target_site_uid':'1000000000000000','authorization_reference':'owned negative fixture'}
    with pytest.raises(HubError,match='ACCESS_DENIED'):service.queue('reader',ident,**request)
    with pytest.raises(HubError,match='SUPERVISED_REVIEW_OR_ARTIFACT_INVALID'):service.queue('maintenance',ident,**request)
    assert not store.query("SELECT * FROM jobs WHERE job_type='tweet_web_publish'")
    assert not store.query('SELECT * FROM publications')


def test_crafted_queue_task_cannot_claim_human_review_or_send(runtime):
    settings,store=runtime;settings.config['web_reviewed_queue_enabled']=True
    parent=store.enqueue('x','123','tweet_process','tweet',{'synthetic':True},'human-review-v1')
    with store.transaction() as db:db.execute("UPDATE jobs SET state='awaiting_review' WHERE job_id=?",(parent,))
    request={'version':'reviewed-web-dispatch-v1','parent_job_id':parent,'manifest_sha256':'0'*64,
        'authorization_reference':'owned negative fixture; never approved',
        'target_site_uid':'1000000000000000','grants_future_content_approval':False,
        'basis':'existing_scoped_owner_review_and_explicit_maintenance_queue_request'}
    child=store.enqueue('reviewed_web',parent,'tweet_web_publish','tweet',request,'human-review-v1')
    assert Worker(settings,store,{'tweet_web_publish':ReviewedWebQueue(settings,store).dispatch}).once('tweet')
    assert store.get(child)['state']=='awaiting_review'
    assert store.get(child)['error_code']=='SUPERVISED_REVIEW_OR_ARTIFACT_INVALID'
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


def test_web_reconciliation_has_separate_bounded_schedule(runtime):
    settings,store=runtime;settings.config['web_reviewed_queue_enabled']=True
    parent=store.enqueue('x','123','tweet_process','tweet',{'synthetic':True},'human-review-v1')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(parent,))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('owned-unknown-key',parent,'bilibili','x','123','site_uid:123','dynamic_website_supervised','unknown',None,None,0))
    scheduler=DurableScheduler(settings,store);scheduler.tick(now=100)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_web_reconcile'"))==1
    assert not store.query("SELECT * FROM jobs WHERE job_type='bilibili_reconcile'")
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',('{"web_reconcile":{"attempts":4}}',parent))
    scheduler.tick(now=1000)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='tweet_web_reconcile'"))==1
    assert not store.query('SELECT * FROM api_calls')
