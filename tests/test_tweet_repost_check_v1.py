"""Owned SQLite/lease/visible-control boundaries, no live platform claims.

The old publisher snapshot/model gate is an explicit upstream seam here;
actual record_visible evidence, publication reservation and worker are real.
"""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import hashlib,json,time,uuid
import pytest

from services.hub.db import encode,Store
from services.hub.errors import HubError
from services.hub.machine_tweets import MachineTweets,KIND,VERSION as MACHINE
from services.hub.tweet_repost_check_v1 import TweetRepostCheck,JOB_TYPE,VERSION,UID,digest,enqueue_check,validate_observation
from services.hub.worker import Worker

PNG=b'\x89PNG\r\n\x1a\n'+b'owned screenshot seam, not platform evidence'


@pytest.fixture
def submission(runtime,monkeypatch):
    settings,store=runtime;clock={'now':1_800_000_000.0};monkeypatch.setattr(time,'time',lambda:clock['now'])
    source='2117189314744590552';external='1266077623570661433';url='https://x.com/example-channel-b/status/'+source
    parent=store.enqueue('x',source,'tweet_process','tweet',{'url':url,'post':{'id':source}},MACHINE)
    area=settings.root/'media'/parent;area.mkdir(parents=True)
    caption='Owned test translation only '+url
    manifest={'job_id':parent,'post_id':source,'caption':caption,'caption_sha256':hashlib.sha256(caption.encode()).hexdigest(),
        'source_url':url,'target_site_uid':UID,'target_name':'Owned test account','render':{'sha256':'a'*64}}
    file=area/'manifest.json';file.write_text(encode(manifest),'utf-8')
    result={'machine_web':{'manifest_file':str(file),'manifest_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'state':'ready','human_review':False}}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='publish_unknown',attempts=2,result=? WHERE job_id=?",(encode(result),parent))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('k'*64,parent,'bilibili','x',source,'site_uid:'+UID,KIND,'unknown',None,None,clock['now']))
    png=area/'published.png';png.write_bytes(PNG)
    evidence={'version':'operator-browser-publication-observation-v1','url':'https://t.bilibili.com/'+external,'site_uid':UID,
        'caption':caption,'artifact_sha256':'a'*64,'screenshot_file':str(png),'screenshot_sha256':hashlib.sha256(PNG).hexdigest(),'visible_in_owner_browser':True}
    efile=area/'visible-result.json';efile.write_text(encode(evidence),'utf-8')
    service=MachineTweets(settings,store)
    monkeypatch.setattr(service,'_snapshot',lambda ident:(store.get(ident),manifest))  # Explicit upstream source/model/render seam.
    return SimpleNamespace(s=settings,db=store,clock=clock,parent=parent,external=external,manifest=manifest,service=service,evidence=efile)


def submit(f):
    result=f.service.record_visible('machine-policy',f.parent,f.evidence)
    f.check=result['repost_check_job_id'];return result


def write_observation(s,spec,assert_lease,*,status='enabled_unverified'):
    assert_lease();area=s.root/'media'/spec['check_job_id']/'tweet-repost-check-v1'/spec['attempt_id'];area.mkdir(parents=True)
    png=area/'observed.png';png.write_bytes(PNG)
    screenshot={'file':str(png),'bytes':len(PNG),'sha256':hashlib.sha256(PNG).hexdigest()}
    body={'version':'tweet-native-repost-observation-v1','request_sha256':digest(spec),'check_job_id':spec['check_job_id'],
        'attempt_id':spec['attempt_id'],'external_id':spec['external_id'],'target_site_uid':UID,'final_url':spec['url'],
        'observed_at':__import__('datetime').datetime.fromtimestamp(time.time(),__import__('datetime').timezone.utc).isoformat(),
        'account_uid':UID,'status':status,'content_binding':{k:True for k in ('exact_dynamic','caption','source_url','author_uid','author_name')},
        'controls':[] if status=='not_found' else [{'visible':True,'disabled':status=='disabled'}],
        'screenshot':screenshot,'actions':{'clicks':0,'file_selections':0,'fills':0,'submits':0},'human_review':False,'platform_cause_verified':False}
    file=area/'observation.json';file.write_text(encode(body),'utf-8');raw=file.read_bytes()
    return {'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def test_visible_submission_and_followup_are_one_transaction_due_in_300_seconds(submission):
    f=submission;result=submit(f);parent=f.db.get(f.parent);check=f.db.get(f.check)
    assert result['state']==parent['state']=='completed' and result['external_id']==f.external
    assert check['next_run']==f.clock['now']+300==check['payload']['not_before'] and check['attempts']==0
    assert check['source_id']==f.external and 'parent_job_id' not in check['payload']
    assert parent['attempts']==2 and len(f.db.query('SELECT * FROM publications'))==1
    assert f.db.claim('tweet',180,4) is None
    f.clock['now']+=300;assert f.db.claim('tweet',180,4)['job_id']==f.check


def test_repeated_record_visible_keeps_original_due_job_and_does_not_reserve_again(submission):
    f=submission;submit(f);old=f.db.get(f.check);f.clock['now']+=30;submit(f)
    assert f.db.get(f.check)==old and len(f.db.query('SELECT * FROM publications'))==1
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)))==1


def test_enqueue_failure_rolls_back_submission_completion_together(submission,monkeypatch):
    f=submission
    import services.hub.tweet_repost_check_v1 as module
    monkeypatch.setattr(module,'enqueue_check',lambda *a,**kw:(_ for _ in ()).throw(OSError('owned fs/db seam')))
    with pytest.raises(OSError):submit(f)
    assert f.db.get(f.parent)['state']=='publish_unknown' and f.db.query('SELECT * FROM publications')[0]['state']=='unknown'
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


@pytest.mark.parametrize('status,expected,suspected',[('enabled_unverified','repost_control_enabled_unverified',False),('disabled','repost_unavailable',True),
    ('auth_required','auth_required',False),('protection_blocked','protection_blocked',False),('wrong_account','wrong_account',False)])
def test_native_outcomes_keep_original_submission_and_never_create_send_permission(submission,status,expected,suspected):
    f=submission;submit(f);f.clock['now']+=300;oldpub=f.db.query('SELECT * FROM publications');parent=f.db.get(f.parent)
    service=TweetRepostCheck(f.s,f.db,observer=lambda s,p,a:write_observation(s,p,a,status=status))
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('tweet')
    saved=f.db.get(f.check);out=saved['result']['final_observation']
    assert saved['state']=='completed' and out['status']==expected and out['suspected_review'] is suspected
    assert out['platform_cause_verified'] is out['public_visibility_verified'] is out['human_review'] is False
    assert f.db.query('SELECT * FROM publications')==oldpub
    current=f.db.get(f.parent)
    assert all(current[k]==parent[k] for k in ('state','stage','attempts','payload','external_publish_id','updated_at'))
    assert current['result']['post_submission_repost_check']==out and not f.db.query('SELECT * FROM api_calls')


def test_not_found_is_distinct_and_bounded_retry_keeps_link_and_saved_evidence(submission):
    f=submission;submit(f);f.clock['now']+=300
    service=TweetRepostCheck(f.s,f.db,observer=lambda s,p,a:write_observation(s,p,a,status='not_found'))
    worker=Worker(f.s,f.db,{JOB_TYPE:service.process})
    for attempt in range(1,min(4,f.s.config['max_attempts'])+1):
        assert worker.once('tweet');saved=f.db.get(f.check)
        assert saved['result']['observations'][-1]['status']=='repost_control_not_found'
        assert saved['result']['observations'][-1]['suspected_review'] is False and saved['attempts']==attempt
        if 'final_observation' not in saved['result']:
            assert saved['state']=='retry_scheduled';f.clock['now']=saved['next_run']
    assert f.db.get(f.check)['state']=='completed' and f.db.get(f.parent)['state']=='completed'
    assert len(f.db.query('SELECT * FROM publications'))==1


def test_lost_child_lease_after_observation_does_not_change_parent(submission):
    f=submission;submit(f);f.clock['now']+=300;job=f.db.claim('tweet',180,4);old=f.db.get(f.parent)
    def stolen(s,p,a):
        ref=write_observation(s,p,a)
        with f.db.transaction() as db:db.execute("UPDATE jobs SET lease_owner='owned-new-worker' WHERE job_id=?",(f.check,))
        return ref
    with pytest.raises(HubError,match='LEASE_LOST'):TweetRepostCheck(f.s,f.db,observer=stolen).process(job)
    assert f.db.get(f.parent)==old and not f.db.query('SELECT * FROM audit WHERE action=?',(VERSION,))


def test_crash_after_durable_observation_reuses_audit_without_second_browser(submission):
    f=submission;submit(f);f.clock['now']+=300;job=f.db.claim('tweet',180,4)
    service=TweetRepostCheck(f.s,f.db,observer=write_observation);result=service.process(job)
    assert f.db.get(f.check)['state']=='processing' and 'final_observation' in result
    fresh=Store(f.s.db_path);fresh.recover('tweet',4,all_processing=True)
    again=TweetRepostCheck(f.s,fresh,observer=lambda *a:pytest.fail('A durable check must not reopen native UI'))
    assert Worker(f.s,fresh,{JOB_TYPE:again.process}).once('tweet')
    assert fresh.get(f.check)['state']=='completed' and fresh.get(f.check)['result']==result


def test_busy_before_browser_is_free_wait_and_never_treats_original_submission_as_new_send(submission,monkeypatch):
    f=submission;submit(f);f.clock['now']+=300;pubs=f.db.query('SELECT * FROM publications')
    from contextlib import contextmanager
    import services.hub.tweet_repost_check_v1 as module
    @contextmanager
    def busy(*a):raise HubError('HEAVY_RESOURCE_BUSY',retryable=True);yield
    monkeypatch.setattr(module,'resource_guard',busy)
    service=TweetRepostCheck(f.s,f.db,observer=lambda *a:pytest.fail('Lock contention must not open native UI'))
    assert Worker(f.s,f.db,{JOB_TYPE:service.process}).once('tweet')
    check=f.db.get(f.check);assert check['state']=='retry_scheduled' and check['attempts']==0
    assert f.db.query('SELECT * FROM publications')==pubs and not f.db.query('SELECT * FROM audit WHERE action=?',(VERSION,))


def test_altered_screenshot_bytes_are_not_a_native_repost_observation(submission):
    f=submission;submit(f);f.clock['now']+=300;job=f.db.claim('tweet',180,4);parent=f.db.get(f.parent)
    def corrupt(s,p,a):
        ref=write_observation(s,p,a);body=json.loads(Path(ref['file']).read_bytes());Path(body['screenshot']['file']).write_bytes(b'wrong screenshot')
        return ref
    with pytest.raises(HubError,match='EVIDENCE_INVALID'):TweetRepostCheck(f.s,f.db,observer=corrupt).process(job)
    assert f.db.get(f.parent)==parent


@pytest.mark.parametrize('change',['parent_id','publication','caption','due','uid'])
def test_changed_bindings_cannot_inspect_another_dynamic(submission,change):
    f=submission;submit(f);f.clock['now']+=300;job=f.db.claim('tweet',180,4)
    if change=='publication':
        with f.db.transaction() as db:db.execute("UPDATE publications SET external_id='1266000000000000000'")
    else:
        p=deepcopy(job['payload'])
        if change=='parent_id':p['submission_job_id']=str(uuid.uuid4())
        elif change=='caption':p['caption']='Other content';p['caption_sha256']=hashlib.sha256(p['caption'].encode()).hexdigest()
        elif change=='due':p['not_before']-=1
        else:p['target_site_uid']='10001'
        with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(p),f.check))
        job=f.db.get(f.check)
    with pytest.raises(HubError):TweetRepostCheck(f.s,f.db,observer=lambda *a:pytest.fail('No browser for changed binding')).process(job)
    assert f.db.get(f.parent)['state']=='completed'


def test_runtime_registers_exact_delayed_type_without_scheduler_dependency(runtime):
    s,db=runtime
    from services.hub.cli import runtime as handlers
    routes=handlers(s,db)
    assert routes[JOB_TYPE].__self__.__class__ is TweetRepostCheck
    assert 'x_official_cached_reparse_v1' in routes and 'x_official_media_ingest_v1' in routes


def test_existing_two_scope_plan_and_owner_report_are_not_native_moderation_or_resend(submission,monkeypatch):
    f=submission;submit(f);pubs=f.db.query('SELECT * FROM publications');old=f.db.get(f.check)
    monkeypatch.setattr(MachineTweets,'_snapshot',lambda self,ident:(f.db.get(ident),f.manifest))  # Explicit old upstream snapshot seam.
    from services.hub.tweet_repost_check_v1 import existing_plan
    before=f.db.query('SELECT * FROM jobs')
    plan=existing_plan(f.s,f.db,f.external);assert plan['state']=='readonly_plan' and f.db.query('SELECT * FROM jobs')==before
    result=existing_plan(f.s,f.db,f.external,apply=True,owner_report_reference='Owned actual-chat-reference seam, not live approval')
    assert result['check_job_id']==f.check and f.db.get(f.check)==old and f.db.query('SELECT * FROM publications')==pubs
    audit=f.db.query("SELECT outcome FROM audit WHERE action='owner_reported_repost_check_requested'")[0]
    outcome=json.loads(audit['outcome']);assert outcome['provenance']=='owner_reported' and outcome['native_observation_claimed'] is False and outcome['platform_cause_verified'] is False
    with pytest.raises(HubError):existing_plan(f.s,f.db,'1266000000000000000',apply=True,owner_report_reference='owned')
