"""Real SQLite scheduling only; no source, model or publication transport."""
from scheduler.service import DurableScheduler
from services.hub.db import encode


def test_legacy_scheduler_queues_one_generic_lookup_for_v4_and_retains_v3_unknown(runtime):
    settings,store=runtime
    settings.config.update(policy_version='machine-tweet-v3-api',machine_tweet_enabled=True)
    parents=[]
    for index,(version,kind) in enumerate((('machine-tweet-v3-api','dynamic_website_machine_v3_api'),
            ('machine-tweet-v4-api-relations','dynamic_website_machine_v4_api_relations'))):
        ident=store.enqueue('x',str(100+index),'tweet_process','tweet',{'owned':True},version);parents.append(ident)
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='publish_unknown',attempts=1,result=? WHERE job_id=?",(encode({'machine_reconcile':{'attempts':0}}),ident))
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('owned-key-'+str(index),ident,'bilibili','x',str(100+index),'site_uid:'+settings.config['target_bilibili_site_uid'],
                 kind,'unknown',None,None,100))
    before=[store.get(ident) for ident in parents]
    scheduler=DurableScheduler(settings,store);scheduler.tick(now=100);scheduler.tick(now=1000)
    queries=store.query("SELECT * FROM jobs WHERE job_type='tweet_machine_reconcile'")
    assert len(queries)==1 and queries[0]['queue']=='tweet' and queries[0]['policy_version']=='machine-tweet-v3-api'
    assert queries[0]['state']=='pending' and queries[0]['attempts']==0
    assert [store.get(ident) for ident in parents]==before
    assert {v['state'] for v in store.query('SELECT * FROM publications')}=={'unknown'}
