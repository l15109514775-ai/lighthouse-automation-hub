"""Owned SQLite/WAL and bounded progress fixtures; no credentials or network."""
from copy import deepcopy
from pathlib import Path
import json
import os
import sqlite3
import uuid
import pytest
from services.hub import observer_snapshot_v1 as observer

NOW=1793689000.0
SOURCE='synthC00003'


def encoded(value):return json.dumps(value,separators=(',',':'))


@pytest.fixture
def owned(tmp_path):
    root=tmp_path/'owned';root.mkdir();(root/'data').mkdir();database=root/'data/hub.sqlite3'
    db=sqlite3.connect(database,isolation_level=None)
    db.executescript('''PRAGMA journal_mode=WAL;
      CREATE TABLE jobs(job_id TEXT PRIMARY KEY,source_platform TEXT,source_id TEXT,job_type TEXT,queue TEXT,state TEXT,
        stage TEXT,attempts INTEGER,error_code TEXT,created_at REAL,updated_at REAL,external_publish_id TEXT,policy_version TEXT,payload TEXT,result TEXT);
      CREATE TABLE pipelines(name TEXT PRIMARY KEY,paused INTEGER);
      CREATE TABLE heartbeats(name TEXT PRIMARY KEY,at REAL);
      CREATE TABLE replay_fifo_v4(candidate_id TEXT PRIMARY KEY,source_id TEXT,state TEXT,reason TEXT,plan_job_id TEXT,download_job_id TEXT,publication_job_id TEXT,refresh_job_id TEXT,updated_at REAL);
      CREATE TABLE publications(job_id TEXT,platform TEXT,state TEXT,external_id TEXT,external_url TEXT,
        idempotency_key TEXT,source_platform TEXT,source_id TEXT,kind TEXT,account_id TEXT);
      CREATE TABLE youtube_videos(video_id TEXT PRIMARY KEY,title TEXT);
      CREATE TABLE api_calls(provider TEXT,started_at REAL,units INTEGER,cost_usd REAL);
      CREATE TABLE replay_part_children_v5(parent_job_id TEXT,part_index INTEGER,child_job_id TEXT);
    ''')
    old,current,planner,legacy=[str(uuid.uuid4()) for _ in range(4)];candidate=str(uuid.uuid4())
    plan={'video_id':SOURCE,'selected_formats':[{'format_id':'299','filesize':2000}],'private_test_field':'secret-value-never-export'}
    binding={'job_id':current,'video_id':SOURCE,'source_plan':plan,'credentials':'secret-value-never-export'}
    state={'youtube_download_v4':{'state':'acquiring','binding':binding},'raw_secret':'secret-value-never-export'}
    for ident,kind,status,attempt,error,result in [(old,'youtube_replay_download_source_revision_history_v1','awaiting_review',4,'YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED',{}),
            (current,'youtube_replay_download_v4','processing',1,None,state),(planner,'youtube_replay_plan_v4','completed',1,None,{}),
            (legacy,'video_process','pending',0,None,{})]:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident,'youtube',SOURCE,kind,'video',status,'youtube_whole_acquiring',attempt,error,NOW-20,NOW-10,None,'replay-fifo-v4',encoded({'cookie':'secret-value-never-export'}),encoded(result)))
    db.executemany('INSERT INTO pipelines VALUES(?,0)',[(q,) for q in ('tweet','video','system')])
    db.executemany('INSERT INTO heartbeats VALUES(?,?)',[(name,NOW-2) for name in observer.BEATS])
    db.execute('INSERT INTO replay_fifo_v4 VALUES(?,?,?,?,?,?,?,?,?)',(candidate,SOURCE,'download_queued',None,planner,current,None,None,NOW-10))
    db.execute('INSERT INTO youtube_videos VALUES(?,?)',(SOURCE,'Owned public replay title'))
    db.execute('INSERT INTO api_calls VALUES(?,?,?,?)',('x',NOW-3,1,.06))
    folder=root/'media'/current/'youtube-whole-v4';folder.mkdir(parents=True)
    progress={'version':'youtube-whole-replay-download-v4','source_plan':plan,'format_id':'299','status':'downloading',
              'downloaded_bytes':1000,'total_bytes':2000,'checked_at':NOW-1,'cookie':'secret-value-never-export',
              'url':'https://example.invalid/?token=secret-value-never-export'}
    (folder/'progress.json').write_text(encoded(progress),encoding='utf-8')
    value={'root':root,'database':database,'db':db,'old':old,'current':current,'planner':planner,'legacy':legacy,'candidate':candidate,'progress':progress,'progress_file':folder/'progress.json','state':state}
    yield value
    db.close()


def collect(case,**kwargs):return observer.collect_snapshot(case['root'],case['database'],now=NOW,**kwargs)
def current(snapshot,case):return next(j for j in snapshot['jobs'] if j['job_id']==case['current'])


def test_projection_uses_current_revision_not_old_failure_or_prototype_and_has_no_secrets(owned,monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('No network is permitted'))
    original=owned['db'].execute('SELECT * FROM jobs ORDER BY job_id').fetchall()
    result=collect(owned);job=current(result,owned)
    assert result['read_only'] is True and result['provider_requests']==0 and result['health']['status']=='ready'
    assert job['is_current'] and not job['is_historical'] and job['progress']=={
        'phase':'download','downloaded_bytes':1000,'total_bytes':2000,'percent':50.0,'observed_at':NOW-1,
        'fresh':True,'format_id':'299','percent_scope':'current_track','reason':None}
    historical=[j for j in result['jobs'] if j['job_id'] in (owned['old'],owned['legacy'])]
    assert len(historical)==2 and all(j['is_historical'] and not j['is_current'] for j in historical)
    assert not result['failures'] and result['daily_report']['job_counts']==[{'state':'completed','count':1},{'state':'processing','count':1}]
    assert result['daily_report']['estimated'] and not result['daily_report']['charges_confirmed']
    wire=encoded(result)
    assert all(v not in wire for v in ('secret-value-never-export','https://example.invalid',str(owned['root']),'"payload"','"result"','"lease_owner"','"credentials"'))
    assert owned['db'].execute('SELECT * FROM jobs ORDER BY job_id').fetchall()==original


def test_readonly_wal_connection_sees_committed_wal_and_holds_consistent_snapshot(owned):
    owned['db'].execute('PRAGMA wal_checkpoint(TRUNCATE)')
    owned['db'].execute("UPDATE jobs SET attempts=2 WHERE job_id=?",(owned['current'],))
    assert Path(str(owned['database'])+'-wal').stat().st_size>0
    with observer.readonly_database(owned['database']) as db:
        assert db.execute('SELECT attempts FROM jobs WHERE job_id=?',(owned['current'],)).fetchone()[0]==2
        with pytest.raises(sqlite3.OperationalError):db.execute("UPDATE jobs SET attempts=0")
        owned['db'].execute('UPDATE jobs SET attempts=3 WHERE job_id=?',(owned['current'],))
        assert db.execute('SELECT attempts FROM jobs WHERE job_id=?',(owned['current'],)).fetchone()[0]==2
    assert current(collect(owned),owned)['attempts']==3


@pytest.mark.parametrize('change',('stale','future','total_changed','plan_changed','wrong_track','boolean_bytes','negative','over_total','wrong_version','binding_other_job','binding_other_source','truncated'))
def test_unverifiable_progress_never_becomes_a_percentage(owned,change):
    body=deepcopy(owned['progress'])
    if change=='stale':body['checked_at']=NOW-121
    elif change=='future':body['checked_at']=NOW+1
    elif change=='total_changed':body['total_bytes']=2001
    elif change=='plan_changed':body['source_plan']['selected_formats'][0]['filesize']=2001
    elif change=='wrong_track':body['format_id']='140'
    elif change=='boolean_bytes':body['downloaded_bytes']=True
    elif change=='negative':body['downloaded_bytes']=-1
    elif change=='over_total':body['downloaded_bytes']=2001
    elif change=='wrong_version':body['version']='invented'
    elif change.startswith('binding_'):
        state=deepcopy(owned['state']);state['youtube_download_v4']['binding']['job_id' if change=='binding_other_job' else 'video_id']='different'
        owned['db'].execute('UPDATE jobs SET result=? WHERE job_id=?',(encoded(state),owned['current']))
    owned['progress_file'].write_text('{' if change=='truncated' else encoded(body),encoding='utf-8')
    projected=current(collect(owned),owned)['progress']
    assert projected['percent'] is None and projected['downloaded_bytes'] is None and projected['fresh'] is False


def test_upload_is_a_stage_not_download_percentage_and_current_failure_remains_visible(owned):
    owned['db'].execute("UPDATE jobs SET job_type='replay_part_publish_v5_p01',stage='replay_part_uploading',state='upload_pending' WHERE job_id=?",(owned['current'],))
    job=current(collect(owned),owned)
    assert job['progress']['phase']=='replay_part_uploading' and job['progress']['percent'] is None
    owned['db'].execute("UPDATE jobs SET state='publish_unknown',error_code='RECONCILE_BEFORE_RETRY' WHERE job_id=?",(owned['current'],))
    failures=collect(owned)['failures'];assert len(failures)==1 and failures[0]['job_id']==owned['current']


def test_current_moderation_child_failure_is_visible_but_replaced_child_is_historical(owned):
    child,previous=str(uuid.uuid4()),str(uuid.uuid4())
    owned['db'].execute('CREATE TABLE replay_part_lookup_schedule_v5(parent_job_id TEXT,active_child_id TEXT)')
    owned['db'].execute('INSERT INTO replay_part_lookup_schedule_v5 VALUES(?,?)',(owned['current'],child))
    for ident in (child,previous):
        owned['db'].execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident,'bilibili',SOURCE,'replay_part_lookup_v5','video','failed','lookup',4,'AUTH_EXPIRED',NOW-2,NOW-1,None,'replay-part-moderation-v5','{}','{}'))
    result=collect(owned)
    assert [j['job_id'] for j in result['failures']]==[child]
    assert next(j for j in result['jobs'] if j['job_id']==previous)['is_historical']


@pytest.mark.parametrize('url,expected',[
    ('https://www.bilibili.com/video/BV15o4M6nEiC/','https://www.bilibili.com/video/BV15o4M6nEiC/'),
    ('https://www.bilibili.com/video/BV15o4M6nEiC/?token=secret-value-never-export',None),
    ('https://evil.invalid/video/BV15o4M6nEiC',None),
])
def test_only_exact_canonical_completed_publication_urls_are_exported(owned,url,expected):
    owned['db'].execute('INSERT INTO publications(job_id,platform,state,external_id,external_url) VALUES(?,?,?,?,?)',(owned['current'],'bilibili','completed','BV15o4M6nEiC',url))
    assert current(collect(owned),owned)['published_url']==expected
    owned['db'].execute("UPDATE publications SET state='awaiting_moderation'")
    assert current(collect(owned),owned)['published_url'] is None


def test_backup_reports_newer_real_failure_and_recomputes_age_without_receipt(owned):
    file=owned['root']/'backup-status.json'
    file.write_text(encoded({'latest_verified':{'updated_at':NOW-100,'age_seconds':1,'receipt':{'SecretKey':'secret-value-never-export'}},
        'last_error':{'updated_at':NOW-10,'error_code':'CLOUDBASE_BACKUP_EVIDENCE_PATH_REJECTED'},'operations':[{'cookie':'secret-value-never-export'}]}),encoding='utf-8')
    result=collect(owned,backup=file)['backup']
    assert result['status']=='last_run_failed' and result['last_verified_age_seconds']==100 and result['last_error_at']==NOW-10
    assert 'secret-value-never-export' not in encoded(result)
    assert collect(owned)['backup']['configured'] is None


def test_stale_heartbeats_and_untrusted_container_snapshot_are_not_ready(owned):
    owned['db'].execute("UPDATE heartbeats SET at=? WHERE name='worker:video'",(NOW-121,))
    (owned['root']/'data/host-status.json').write_text(encoded({'checked_at':NOW-181,'containers':[]}),encoding='utf-8')
    result=collect(owned)
    assert result['health']['status']=='degraded' and result['health']['stale_services']==['worker:video']
    assert not result['containers']['available']


def test_recent_job_listing_is_bounded_and_retains_active_fifo_references(owned):
    for i in range(280):
        owned['db'].execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()),'x',str(2110000000000000000+i),'tweet_process','tweet','completed','done',1,None,NOW+i,NOW,None,'machine-tweet-v4-api-relations','{}','{}'))
    result=collect(owned)
    assert len(result['jobs'])<=observer.MAX_JOBS and current(result,owned)['is_current']


def test_atomic_output_replaces_only_snapshot_and_is_group_readable_under_private_umask(owned):
    output=owned['root']/'status.json';output.write_text('old',encoding='utf-8')
    previous=os.umask(0o077)
    try:observer.write_snapshot(output,collect(owned))
    finally:os.umask(previous)
    assert json.loads(output.read_bytes())['version']==observer.VERSION
    assert not list(owned['root'].glob('.observer-*.tmp'))
    if os.name=='posix':assert output.stat().st_mode&0o777==0o640
    before=output.read_bytes()
    with pytest.raises(ValueError):observer.write_snapshot(output,{'large':'x'*4_194_305})
    assert output.read_bytes()==before


def test_missing_database_is_not_created_and_invalid_snapshot_is_not_written(tmp_path):
    missing=tmp_path/'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        with observer.readonly_database(missing):pass
    assert not missing.exists()


def add_job(case, kind, *, platform='youtube', source=SOURCE, queue='video', policy='replay-fifo-v4',
            status='failed', payload=None, external=None, created_at=NOW-2):
    ident=str(uuid.uuid4())
    case['db'].execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (ident,platform,source,kind,queue,status,'observed',1,'OWNED_ERROR',created_at,NOW-1,external,policy,encoded(payload or {}),'{}'))
    return ident


def test_latest_video_queue_discovery_is_current_and_prior_periodic_is_history(owned):
    old=add_job(owned,'youtube_discovery',platform='scheduler',policy='live-policy',created_at=NOW-4)
    new=add_job(owned,'youtube_discovery',platform='scheduler',policy='live-policy')
    result=collect(owned)
    assert next(j for j in result['jobs'] if j['job_id']==old)['is_historical']
    assert [j['job_id'] for j in result['failures']]==[new]


def test_only_fifo_current_refresh_is_retained_even_outside_recent_jobs(owned):
    old=add_job(owned,'youtube_replay_refresh_v4',platform='maintenance',created_at=NOW-500)
    new=add_job(owned,'youtube_replay_refresh_v4',platform='maintenance',created_at=NOW-501)
    owned['db'].execute('UPDATE replay_fifo_v4 SET refresh_job_id=?',(new,))
    for index in range(101): add_job(owned,'video_process',created_at=NOW+index)
    result=collect(owned)
    assert result['replays'][0]['refresh_job_id']==new
    assert [j['job_id'] for j in result['failures']]==[new]
    assert old not in {j['job_id'] for j in result['jobs']}


def bound_repost(case):
    external='1266193067951521812';source='2117280948459094296';key='owned-publication-key'
    parent=add_job(case,'tweet_process',platform='x',source=source,queue='tweet',status='completed',
        policy='machine-tweet-v4-api-relations',external=external)
    case['db'].execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?)',
        (parent,'bilibili','completed',external,'https://t.bilibili.com/'+external,key,'x',source,
         'dynamic_website_machine_v4_api_relations','site_uid:1000000000000000'))
    request={'version':'tweet-native-repost-check-v1','external_id':external,'submission_job_id':parent,
        'source_id':source,'publication_key':key,'publication_kind':'dynamic_website_machine_v4_api_relations',
        'target_site_uid':'1000000000000000','caption':'secret-value-never-export'}
    check=add_job(case,'tweet_repost_check_v1',platform='bilibili',source=external,queue='tweet',
        policy='tweet-native-repost-check-v1',payload=request)
    return check,request


@pytest.mark.parametrize('change',('none','wrong_parent','wrong_publication','wrong_account','wrong_source','wrong_external','malformed','invalid_key_type'))
def test_repost_check_is_current_only_with_real_parent_and_publication_binding(owned,change):
    ident,request=bound_repost(owned)
    if change=='wrong_parent': request['submission_job_id']=str(uuid.uuid4())
    elif change=='wrong_publication': request['publication_key']='not-this-publication'
    elif change=='wrong_account': owned['db'].execute("UPDATE publications SET account_id='site_uid:1'")
    elif change=='wrong_source': request['source_id']='2110000000000000000'
    elif change=='wrong_external': request['external_id']='1260000000000000000'
    elif change=='invalid_key_type': request['publication_key']={'credential':'secret-value-never-export'}
    owned['db'].execute('UPDATE jobs SET payload=? WHERE job_id=?',('{' if change=='malformed' else encoded(request),ident))
    result=collect(owned);job=next(j for j in result['jobs'] if j['job_id']==ident)
    assert job['is_current']==(change=='none') and job['is_historical']==(change!='none')
    assert [j['job_id'] for j in result['failures']]==([ident] if change=='none' else [])
    assert 'secret-value-never-export' not in encoded(result)


@pytest.mark.parametrize('change',('valid','wrong_previous','invalid_type'))
def test_reinspection_supersedes_only_the_matching_prior_check(owned,change):
    valid=change=='valid'
    old,request=bound_repost(owned)
    request=deepcopy(request);request['reinspection']={'version':'tweet-native-repost-reinspection-v2',
        'previous_check_job_id':old if valid else str(uuid.uuid4()),'previous_result_sha256':'a'*64,
        'owner_report_reference':'secret-value-never-export'}
    if change=='invalid_type': request['reinspection']=['secret-value-never-export']
    new=add_job(owned,'tweet_repost_check_v1',platform='bilibili',source=request['external_id'],queue='tweet',
        policy='tweet-native-repost-reinspection-v2',payload=request,created_at=NOW-1)
    result=collect(owned)
    assert [j['job_id'] for j in result['failures']]==[new if valid else old]
    assert next(j for j in result['jobs'] if j['job_id']==(old if valid else new))['is_historical']


def test_document_reader_uses_nonblocking_open_before_regular_file_validation(tmp_path,monkeypatch):
    path=tmp_path/'document.json';path.write_text('{}',encoding='utf-8');seen=[]
    flag=getattr(os,'O_NONBLOCK',1<<30);monkeypatch.setattr(os,'O_NONBLOCK',flag,raising=False)
    def capture(file,flags):
        seen.append(flags);raise OSError('owned-open-stop')
    monkeypatch.setattr(os,'open',capture)
    with pytest.raises(OSError,match='owned-open-stop'): observer.read_json(path)
    assert seen and seen[0]&flag


@pytest.mark.parametrize('date',('2026-11-03T00:00:01','2026-11-01T00:00:01'))
def test_api_usage_uses_beijing_day_and_month_boundaries_without_resetting_records(owned,date):
    local=observer.datetime.fromisoformat(date).replace(tzinfo=observer.CN)
    day=local.replace(hour=0,minute=0,second=0);month=day.replace(day=1)
    rows=[('x',day.timestamp(),1,.01),('x',local.timestamp(),1,.02),
          ('x',month.timestamp()-1,1,.08)]
    if day!=month: rows.append(('x',month.timestamp(),1,.04))
    owned['db'].execute('DELETE FROM api_calls');owned['db'].executemany('INSERT INTO api_calls VALUES(?,?,?,?)',rows)
    report=observer.collect_snapshot(owned['root'],owned['database'],now=local.timestamp())['daily_report']
    assert report['date']==local.date().isoformat() and report['timezone']=='Asia/Shanghai'
    assert report['api_usage']['daily']==[{'provider':'x','calls':2,'quota_units':2,'reserved_upper_bound_usd':.03}]
    month_usage=report['api_usage']['monthly'][0]
    assert month_usage['calls']==(3 if day!=month else 2)
    assert month_usage['reserved_upper_bound_usd']==pytest.approx(.07 if day!=month else .03)
    assert owned['db'].execute('SELECT COUNT(*) FROM api_calls').fetchone()[0]==len(rows)


def bind_translation(owned, *, revision_state='completed', revision_error=None,
                     parent_state='completed', publication_state='completed',
                     external='1266459648005767233', external_url=None,
                     role='post', revision_suffix=':revision-1'):
    parent, base, revision = [str(uuid.uuid4()) for _ in range(3)]
    rows = [
        (parent, 'x', '2117588487524515959', 'tweet_process', 'tweet', parent_state,
         'machine_publication_visible', 1, None if parent_state == 'completed' else 'OWNED_PARENT_PENDING',
         NOW-30, NOW-10, external if parent_state == 'completed' else None,
         'machine-tweet-v4-api-relations', encoded({'text': 'owned-translation-source'}), encoded({})),
        (base, 'machine_model', parent + ':' + role, 'machine_translation_node', 'tweet', 'failed',
         'model_quality_rejected', 0, 'MACHINE_TRANSLATION_CHECK_FAILED', NOW-20, NOW-9, None,
         'machine-tweet-v4-api-relations', encoded({}), encoded({})),
        (revision, 'machine_model', parent + ':' + role + revision_suffix, 'machine_translation_node', 'tweet',
         revision_state, 'model_receipts_verified' if revision_state == 'completed' else 'model_quality_rejected',
         0, revision_error, NOW-15, NOW-8, None, 'machine-tweet-v4-api-relations', encoded({}), encoded({})),
    ]
    for row in rows:
        owned['db'].execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
    owned['db'].execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?)',
        (parent, 'bilibili', publication_state, external,
         external_url or ('https://t.bilibili.com/' + external), 'owned-publication-key', 'x',
         '2117588487524515959', 'dynamic_website_machine_v4_api_relations', 'site_uid:1000000000000000'))
    return parent, base, revision


def test_superseded_base_translation_reports_revision_and_publication_and_leaves_no_failure(owned):
    parent, base, revision = bind_translation(owned)
    result = collect(owned)
    base_item = next(j for j in result['jobs'] if j['job_id'] == base)
    assert base_item['state'] == 'failed' and base_item['error_code'] == 'MACHINE_TRANSLATION_CHECK_FAILED'
    assert base_item['resolution'] == {
        'kind': 'superseded_by_revision', 'revision_job_id': revision, 'revision_state': 'completed',
        'revision_stage': 'model_receipts_verified', 'revision_error_code': None,
        'revision_updated_at': NOW-8, 'parent_job_id': parent, 'parent_state': 'completed',
        'published_url': 'https://t.bilibili.com/1266459648005767233', 'supersession_complete': True}
    assert next(j for j in result['jobs'] if j['job_id'] == revision)['resolution'] is None
    assert [j['job_id'] for j in result['failures']] == []


def test_failed_revision_keeps_base_translation_actionable(owned):
    parent, base, revision = bind_translation(owned, revision_state='failed',
        revision_error='MACHINE_TRANSLATION_CHECK_FAILED')
    result = collect(owned)
    item = next(j for j in result['jobs'] if j['job_id'] == base)['resolution']
    assert item['supersession_complete'] is False and item['published_url'] is None
    assert item['revision_error_code'] == 'MACHINE_TRANSLATION_CHECK_FAILED'
    assert {j['job_id'] for j in result['failures']} == {base, revision}


def test_base_without_revision_has_no_resolution_and_stays_actionable(owned):
    parent, base, revision = bind_translation(owned)
    owned['db'].execute('DELETE FROM jobs WHERE job_id=?', (revision,))
    result = collect(owned)
    item = next(j for j in result['jobs'] if j['job_id'] == base)
    assert item['resolution'] is None and [j['job_id'] for j in result['failures']] == [base]


def test_only_first_revision_supersedes_base_translation(owned):
    parent, base, revision = bind_translation(owned, revision_suffix=':revision-2')
    result = collect(owned)
    item = next(j for j in result['jobs'] if j['job_id'] == base)
    assert item['resolution'] is None and base in {j['job_id'] for j in result['failures']}


@pytest.mark.parametrize('parent_state,publication_state', [('processing', 'completed'), ('completed', 'awaiting_moderation')])
def test_uncompleted_parent_or_publication_does_not_supersede(owned, parent_state, publication_state):
    parent, base, revision = bind_translation(owned, parent_state=parent_state, publication_state=publication_state)
    result = collect(owned)
    item = next(j for j in result['jobs'] if j['job_id'] == base)['resolution']
    assert item['supersession_complete'] is False and item['published_url'] is None
    assert base in {j['job_id'] for j in result['failures']}


def test_resolution_never_leaks_noncanonical_or_credentialed_urls(owned):
    secret = 'secret-value-never-export'
    parent, base, revision = bind_translation(owned,
        external_url='https://t.bilibili.com/1266459648005767233?token=' + secret)
    result = collect(owned)
    item = next(j for j in result['jobs'] if j['job_id'] == base)['resolution']
    assert item['supersession_complete'] is False and item['published_url'] is None
    assert base in {j['job_id'] for j in result['failures']}
    assert secret not in encoded(result) and secret not in encoded(item)
