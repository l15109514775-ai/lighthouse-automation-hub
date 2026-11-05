"""Real SQLite/Worker reinspection; original publisher/source is the owned seam.

One Worker case also consumes actual owned Chromium v2 evidence. No platform,
source API, send handler or original-job retry is invoked.
"""
import hashlib,json,os,subprocess,time
from pathlib import Path
import pytest
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.machine_tweets import MachineTweets
from services.hub.tweet_repost_check_v1 import JOB_TYPE,VERSION,REINSPECTION,TweetRepostCheck,reinspection_plan,digest
from services.hub.worker import Worker
from tests.test_tweet_repost_check_v1 import submission,submit,write_observation

REAL_TIME=time.time
OWNER='Owned explicit two-dynamic check reference; no send authorization'


@pytest.fixture
def prior(submission,monkeypatch):
    f=submission;f.clock['now']=float(int(REAL_TIME())-1000);submit(f);f.clock['now']+=300
    service=TweetRepostCheck(f.s,f.db,observer=lambda s,p,a:write_observation(s,p,a,status='ui_unrecognized'))
    worker=Worker(f.s,f.db,{JOB_TYPE:service.process})
    for i in range(4):
        assert worker.once('tweet'),(i,f.clock['now'],{k:f.db.get(f.check)[k] for k in ('state','error_code','next_run','attempts')})
        old=f.db.get(f.check)
        if i<3:f.clock['now']=old['next_run']
    assert old['state']=='completed' and old['attempts']==4
    # Preserve the exact deployed-v1 result shape (its raw JSON is unchanged).
    # This is an owned legacy-format fixture, not fabricated live evidence.
    extra=('author_navigation_click_attempts','total_native_click_attempts','native_observation_version')
    with f.db.transaction() as db:
        value=f.db.get(f.check)
        for item in [*value['result']['observations'],value['result']['final_observation']]:
            for key in extra:item.pop(key,None)
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value['result']),f.check))
        for row in db.execute('SELECT seq,outcome FROM audit WHERE action=? AND target=?',(VERSION,f.check)).fetchall():
            body=json.loads(row['outcome'])
            for key in extra:body.pop(key,None)
            db.execute('UPDATE audit SET outcome=? WHERE seq=?',(encode(body),row['seq']))
        parent=f.db.get(f.parent)
        for key in extra:parent['result']['post_submission_repost_check'].pop(key,None)
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(parent['result']),f.parent))
    monkeypatch.setattr(MachineTweets,'_snapshot',lambda self,ident:(f.db.get(ident),f.manifest))
    f.old=f.db.get(f.check);f.parent_before=f.db.get(f.parent);f.pubs=f.db.query('SELECT * FROM publications')
    return f


def queue(f):
    return reinspection_plan(f.s,f.db,f.external,apply=True,owner_report_reference=OWNER)['check_job_id']


def v2_owned_observer(settings,spec,check):
    ref=write_observation(settings,spec,check)
    body=json.loads(Path(ref['file']).read_bytes());body['version']='tweet-native-repost-observation-v2'
    body['actions'].update(author_navigation_clicks=0,repost_dialog_clicks=0)
    body['author_identity']={'version':'tweet-native-author-identity-v2','method':'visible_named_author_anchor',
        'site_uid':spec['target_site_uid'],'profile_url':'https://space.bilibili.com/'+spec['target_site_uid'],
        'source_dynamic_url':spec['url'],'observed_at':body['observed_at']}
    raw=encode(body).encode();Path(ref['file']).write_bytes(raw)
    return {**ref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def test_readonly_plan_uses_real_old_four_zero_action_receipts_and_fixed_scope(prior):
    f=prior;before=f.db.query('SELECT * FROM jobs')
    plan=reinspection_plan(f.s,f.db,f.external)
    assert plan['state']=='readonly_plan' and plan['previous_check_job_id']==f.check and plan['previous_attempts']==4
    assert plan['previous_result_sha256']==digest(f.old['result']) and f.db.query('SELECT * FROM jobs')==before
    with pytest.raises(HubError):reinspection_plan(f.s,f.db,'1266000000000000000',apply=True,owner_report_reference=OWNER)


def test_one_new_policy_slot_is_idempotent_and_does_not_reset_old_job_or_marker(prior):
    f=prior;ident=queue(f);again=queue(f);assert ident==again and ident!=f.check
    fresh=f.db.get(ident)
    assert fresh['job_type']==JOB_TYPE and fresh['policy_version']==REINSPECTION and fresh['source_id']==f.external
    assert fresh['attempts']==0 and fresh['state']=='pending' and fresh['payload']['not_before']==f.old['payload']['not_before']
    assert fresh['payload']['reinspection']['previous_result_sha256']==digest(f.old['result'])
    assert f.db.get(f.check)==f.old and f.db.get(f.parent)==f.parent_before and f.db.query('SELECT * FROM publications')==f.pubs
    assert len(f.db.query("SELECT * FROM jobs WHERE job_type=? AND source_id=?",(JOB_TYPE,f.external)))==2
    assert len(f.db.query("SELECT * FROM audit WHERE action='queue_tweet_repost_reinspection_v2'"))==1


@pytest.mark.parametrize('change',['attempts','different_result'])
def test_only_the_completed_four_attempt_unrecognized_history_may_be_rechecked(prior,change):
    f=prior
    with f.db.transaction() as db:
        if change=='attempts':db.execute('UPDATE jobs SET attempts=3 WHERE job_id=?',(f.check,))
        else:
            value=f.db.get(f.check)['result'];value['final_observation']['status']='repost_unavailable'
            db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value),f.check))
    with pytest.raises(HubError):queue(f)
    assert not f.db.query('SELECT * FROM jobs WHERE policy_version=?',(REINSPECTION,))


def test_queued_reinspection_refuses_changed_original_result(prior):
    f=prior;ident=queue(f)
    with f.db.transaction() as db:
        value=f.db.get(f.check)['result'];value['unexpected_after_enqueue']=True
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(value),f.check))
    job=f.db.claim('tweet',180,4);assert job['job_id']==ident
    with pytest.raises(HubError):TweetRepostCheck(f.s,f.db,observer=lambda *a:pytest.fail('No browser after history drift')).process(job)
    assert f.db.query('SELECT * FROM publications')==f.pubs


def test_normal_worker_consumes_actual_chromium_v2_without_any_send_handler(prior,monkeypatch):
    f=prior;ident=queue(f);monkeypatch.setattr(time,'time',REAL_TIME)
    repo=Path(__file__).resolve().parents[1]
    def observer(settings,spec,check):
        check();request=settings.root/'request-v2.json';request.write_text(encode(spec),encoding='utf-8')
        folder=settings.root/'media'/spec['check_job_id']/'tweet-repost-check-v1'/spec['attempt_id']
        env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME') if k in os.environ}
        env['PLAYWRIGHT_BROWSERS_PATH']=str(repo/'compositor/runtime/browsers')
        p=subprocess.run(['node',str(repo/'tests/tweet_repost_author_navigation_v2_fixture.mjs'),'popup',str(folder),str(request)],
            cwd=repo,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
        assert p.returncode==0,p.stderr
        result=json.loads(p.stdout.strip().splitlines()[-1]);assert result['counts']=={'author':1,'repost':1,'final':0}
        return result['result_reference']
    service=TweetRepostCheck(f.s,f.db,observer=observer)
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('tweet')
    fresh=f.db.get(ident);assert fresh['state']=='completed' and fresh['attempts']==1
    assert fresh['result']['final_observation']['status']=='repost_dialog_opened_no_send'
    parent=f.db.get(f.parent)
    assert parent['result']['post_submission_repost_check']==f.parent_before['result']['post_submission_repost_check']
    assert parent['result']['post_submission_repost_reinspection_v2']==fresh['result']['final_observation']
    assert f.db.get(f.check)==f.old and f.db.query('SELECT * FROM publications')==f.pubs


def test_crash_after_new_durable_observation_reuses_its_separate_audit(prior):
    f=prior;ident=queue(f);job=f.db.claim('tweet',180,4);assert job['job_id']==ident
    service=TweetRepostCheck(f.s,f.db,observer=v2_owned_observer);saved=service.process(job)
    assert f.db.get(ident)['state']=='processing'
    service.observer=lambda *a:pytest.fail('Durable v2 result must not reopen a browser')
    assert service.process(f.db.get(ident))==saved
    assert len(f.db.query('SELECT * FROM audit WHERE action=? AND target=?',(REINSPECTION,ident)))==1
    assert f.db.get(f.check)==f.old and f.db.query('SELECT * FROM publications')==f.pubs


def test_a_v2_queue_job_cannot_reuse_old_v1_observation_as_its_new_check(prior):
    f=prior;ident=queue(f)
    service=TweetRepostCheck(f.s,f.db,observer=write_observation)
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('tweet')
    assert f.db.get(ident)['error_code']=='TWEET_REPOST_REINSPECTION_V2_EVIDENCE_REQUIRED'
    assert f.db.get(f.check)==f.old and f.db.get(f.parent)==f.parent_before
    assert f.db.query('SELECT * FROM publications')==f.pubs
