"""Owned runtime/SQLite only. No real consumer, key, browser or download."""
from copy import deepcopy
from types import SimpleNamespace
import uuid
import pytest

from test_short_video_web import short_video,PRINCIPAL
from test_replay_highest_video_v2 import highest,UID
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub import replay_highest_worker_runtime_v2 as rt
from services.hub.replay_highest_video_v2 import STATE,KIND,POLICY,JOB_TYPE,DOWNLOAD_STATE,DOWNLOAD_JOB_TYPE


@pytest.fixture
def known(highest):
    f=highest;job=f['service'].create_candidate(PRINCIPAL,f['source'],f['metadata'])
    current=f['db'].get(job);binding=current['payload'];state=current['result'][STATE];attempt=str(uuid.uuid4());bv='BV0000000000'
    state.update(state='awaiting_moderation',attempt_id=attempt,result_observation={'version':'replay-highest-browser-result-v2',
        'attempt_id':attempt,'site_uid':UID,'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256'],
        'external_id':bv,'url':'https://www.bilibili.com/video/'+bv,'state':'awaiting_moderation'})
    with f['db'].transaction() as con:
        con.execute("UPDATE jobs SET state='awaiting_moderation',updated_at=1000,attempts=1,result=?,external_publish_id=? WHERE job_id=?",
            (encode(current['result']),bv,job))
        con.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(state['key'],job,'bilibili','youtube',rt.PILOT_SOURCE,
            'site_uid:'+UID,KIND,'awaiting_moderation',bv,'https://www.bilibili.com/video/'+bv,1000))
        con.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(1000,'Owned only','short_video_reserve_submit',?,'one_artifact_one_click_reserved')",(job,))
    return f,job


def test_bounded_tick_only_schedules_existing_bv_without_reset_or_media_read(known):
    f,job=known;before=f['db'].get(job);publications=f['db'].query('SELECT * FROM publications')
    f['file'].unlink()  # Result reconciliation must survive authorized later cleanup.
    result=rt.reconcile_tick(f['s'],f['db'],f['service'],now=1401)
    assert result['scheduled_known_bv_lookups']==[job]
    after=f['db'].get(job)
    assert after['state']=='retry_scheduled' and after['attempts']==before['attempts']
    for key in before:
        if key not in ('state','updated_at','next_run','stage'):assert before[key]==after[key]
    assert f['db'].query('SELECT * FROM publications')==publications
    assert rt.reconcile_tick(f['s'],f['db'],f['service'],now=1402)['scheduled_known_bv_lookups']==[]
    assert result['new_sources_admitted']==0 and result['unknown_submissions_changed'] is False


@pytest.mark.parametrize('change',['unknown','no_bv','wrong_uid','attempt_limit','old_start'])
def test_unknown_unbound_over_budget_or_pre_november_work_is_never_requeued(known,change):
    f,job=known;current=f['db'].get(job)
    with f['db'].transaction() as con:
        if change=='unknown':con.execute("UPDATE jobs SET state='publish_unknown' WHERE job_id=?",(job,))
        elif change=='no_bv':con.execute('UPDATE jobs SET external_publish_id=NULL WHERE job_id=?',(job,))
        elif change=='attempt_limit':con.execute('UPDATE jobs SET attempts=? WHERE job_id=?',(f['s'].config['max_attempts'],job))
        elif change=='wrong_uid':
            current['payload']['site_uid']='999';con.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(current['payload']),job))
        else:
            source=f['db'].get(f['source']);source['payload']['raw']['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
            con.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(source['payload']),f['source']))
    before=f['db'].get(job);audit=f['db'].query('SELECT * FROM audit')
    assert rt.reconcile_tick(f['s'],f['db'],f['service'],now=1401)['scheduled_known_bv_lookups']==[]
    assert f['db'].get(job)==before and f['db'].query('SELECT * FROM audit')==audit


def test_guard_and_loop_keep_source_scope_separate_from_all_source_automation(highest,monkeypatch):
    f=highest;job=f['service'].create_candidate(PRINCIPAL,f['source'],f['metadata']);calls=[]
    wrapped=rt.guarded_handler(f['service'],lambda current:calls.append(current['job_id']) or 'Owned result')
    assert wrapped(f['db'].get(job))=='Owned result'
    modified=f['db'].get(job);modified['source_id']='0H6_bTvWeDQ'
    with pytest.raises(HubError,match='OUTSIDE_PILOT'):wrapped(modified)
    assert calls==[job]
    monkeypatch.setattr(rt,'reconcile_tick',lambda *args,**kwargs:calls.append('bounded tick'))
    worker=SimpleNamespace(once=lambda queue:calls.append(queue) or False)
    assert rt.run_loop(f['s'],f['db'],worker,f['service'],once=True,clock=lambda:0,sleep=lambda n:pytest.fail('once sleeps')) is False
    assert calls[-2:]==['bounded tick','video']


def test_registration_preserves_legacy_handlers_and_adds_both_new_kinds(highest,monkeypatch):
    from services.hub import cli,replay_video_browser,youtube_download_v2
    f=highest;legacy=lambda job:'Owned legacy handler'
    monkeypatch.setattr(cli,'runtime',lambda *args:{'youtube_download':legacy,'owned_legacy':legacy})
    monkeypatch.setattr(youtube_download_v2.YouTubeHighestDownload,'policy',lambda self:{'owned_only':True})
    monkeypatch.setattr(replay_video_browser,'ReplayVideoWorkerHandler',lambda *args,**kwargs:SimpleNamespace(process=lambda job:'Owned new handler'))
    worker,service=rt.build_worker(f['s'],f['db'])
    assert worker.handlers['owned_legacy'] is worker.handlers['youtube_download'] is legacy
    assert set(worker.handlers)=={'owned_legacy','youtube_download','replay_video_publish',JOB_TYPE,DOWNLOAD_JOB_TYPE}
    assert worker.handlers[DOWNLOAD_JOB_TYPE].__self__.__class__ is youtube_download_v2.YouTubeHighestDownload
    assert worker.handlers[JOB_TYPE](f['db'].get(service.create_candidate(PRINCIPAL,f['source'],f['metadata'])))=='Owned new handler'
    assert not f['db'].query('SELECT * FROM publications')
