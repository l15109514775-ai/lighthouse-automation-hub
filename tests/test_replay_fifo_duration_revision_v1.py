"""Actual two official duration revisions; owned inventory, real FIFO/Worker/SQLite.

The public API snapshots are recorded input, not new HTTP or media evidence.
The acquisition seam supplies owned format inventory; all planning/binding gates
run unchanged, including the extractor's two-second duration check.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json,time
from pathlib import Path

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_fifo_v4 import PLAN_JOB,DOWNLOAD_JOB,PLANNING_SOURCE,sha
from services.hub.replay_multipart_v4 import plan_whole_replay
from services.hub.worker import Worker
from services.hub.youtube_download_v4 import YouTubeWholeReplayPrepare,YouTubeWholeReplayDownload,VERSION
from test_replay_fifo_v4 import fifo,seed
from test_youtube_download_v4 import configure_v4
from test_youtube_natural_plan_v3 import fixture as inventory_fixture


ACTUAL=json.loads((Path(__file__).parent/'fixtures/replay_fifo_duration_actual_20261103.json').read_bytes())['records']


def write_observation(f,raw,*,observed=None):
    with f.db.transaction() as db:
        db.execute('UPDATE youtube_videos SET metadata=?,title=?,published_at=?,duration=?,actual_start=?,actual_end=?,last_api_check=? WHERE video_id=?',
            (encode(raw),raw['snippet']['title'],raw['snippet']['publishedAt'],raw['contentDetails']['duration'],
             raw['liveStreamingDetails']['actualStartTime'],raw['liveStreamingDetails']['actualEndTime'],
             f.clock[0] if observed is None else observed,raw['id']))


def actual(f,record):
    f=configure_v4(f);old=record['registered_payload'];raw=old['raw'];live=raw['liveStreamingDetails']
    with f.db.transaction() as db:
        db.execute("""INSERT INTO youtube_videos(video_id,channel_id,title,published_at,url,state,actual_start,actual_end,duration,
            first_seen_at,last_seen_at,last_api_check,source_api,metadata) VALUES(?,?,?,?,?,'ended',?,?,?,?,?,?,'youtube-data-v3-official',?)""",
            (raw['id'],raw['snippet']['channelId'],raw['snippet']['title'],raw['snippet']['publishedAt'],old['url'],
             live['actualStartTime'],live['actualEndTime'],old['duration'],f.clock[0],f.clock[0],f.clock[0],encode(raw)))
    cid=f.q.register_all_observed()['registered'][0];c=f.q.candidate(cid)
    assert c['identity_sha256']==record['registered_identity_sha256']
    assert c['source_metadata_sha256']==record['registered_metadata_sha256']
    write_observation(f,record['official_raw'])
    return f,c


def prepare(f,c):
    _,info=inventory_fixture();raw=json.loads(f.db.query('SELECT metadata FROM youtube_videos WHERE video_id=?',(c['source_id'],))[0]['metadata'])
    info.update(id=c['source_id'],channel_id=c['channel_id'],duration=f.q._source(f.q._fresh(c,f.q.policy()),f.q.policy())[0]['duration'])
    calls=[]
    class OwnedInventory:
        def acquire(self,job,binding,*,check):
            check();calls.append(deepcopy(binding))
            plan=plan_whole_replay(binding['official_raw'],info,observed_at=datetime.now(timezone.utc).isoformat(),policy=f.whole)
            return {'version':VERSION,'binding_sha256':sha(binding),'source_plan':plan,'media_downloaded':False,
                'human_review':False,'publication_authorized':False}
    service=YouTubeWholeReplayPrepare(f.s,f.db,downloader_factory=lambda _:OwnedInventory())
    jid=f.q.enqueue_plan_tick(f.handlers)['created'][0];original=f.db.get(jid)['payload']
    assert Worker(f.s,f.db,{PLAN_JOB:service.process}).once('video')
    job=f.db.get(jid);assert job['state']=='completed',job['error_code']
    assert job['attempts']==1 and job['payload']==original
    return job,calls,service


@pytest.mark.parametrize('record',ACTUAL,ids=lambda r:r['source_id'])
def test_actual_revisions_pass_real_plan_download_binding_without_rewriting_registration(fifo,record):
    f,c=actual(fifo,record);job,calls,service=prepare(f,c)
    proof=job['result'][PLANNING_SOURCE];plan=job['result']['replay_fifo_plan_v4']['plan']
    expected_delta=2 if record['source_id']=='synthA00001' else -4
    assert proof['duration_delta_seconds']==expected_delta and proof['maximum_revision_seconds']==5
    assert proof['official_raw']==record['official_raw'] and calls[0]['official_raw']==record['official_raw']
    assert plan['duration']==proof['observed_duration_seconds']
    assert plan['source_plan_basis']['official_metadata_sha256']==sha(record['official_raw'])
    after=f.q.candidate(c['candidate_id'])
    for key in ('source_payload','identity_sha256','source_metadata_sha256','registered_at','actual_start','actual_end'):
        assert after[key]==c[key]
    made=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15)['created'];assert len(made)==1
    download=f.db.claim('video',600,4);assert download['job_id']==made[0]
    assert download['payload']['raw']==record['official_raw'] and download['payload'][PLANNING_SOURCE]==proof
    acquiring=YouTubeWholeReplayDownload(f.s,f.db);_,binding=acquiring._binding(download)
    assert binding['duration']==proof['observed_duration_seconds'] and binding['official_raw']==record['official_raw']
    ref=acquiring.fresh_observation(download,binding);body=json.loads(Path(ref['file']).read_bytes())
    assert body['duration_identity_observation']==proof
    assert not f.db.query('SELECT * FROM publications') and not f.db.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('field',['id','channel','start','end','private','row_raw'])
def test_stable_identity_and_row_raw_guards_still_reject(fifo,field):
    f,c=actual(fifo,ACTUAL[0]);row=f.db.query('SELECT * FROM youtube_videos')[0];raw=json.loads(row['metadata'])
    if field=='id':raw['id']='foreign0000'
    elif field=='channel':raw['snippet']['channelId']='UC'+'x'*22;row['channel_id']=raw['snippet']['channelId']
    elif field in ('start','end'):
        key='actualStartTime' if field=='start' else 'actualEndTime';raw['liveStreamingDetails'][key]='2026-11-02T15:36:31Z'
        row['actual_start' if field=='start' else 'actual_end']=raw['liveStreamingDetails'][key]
    elif field=='private':raw['status']['privacyStatus']='private'
    else:row['duration']='PT1S'
    row['metadata']=encode(raw)
    with pytest.raises(HubError):f.q.observed_identity(c,row,f.q.policy())
    assert not f.db.query('SELECT * FROM jobs') and not f.db.query('SELECT * FROM replay_fifo_dispatches_v4')


@pytest.mark.parametrize('delta',[-6,6,-10000])
def test_large_or_trimmed_duration_is_not_same_source(fifo,delta):
    f,c=actual(fifo,ACTUAL[0]);row=f.db.query('SELECT * FROM youtube_videos')[0];raw=json.loads(row['metadata'])
    raw['contentDetails']['duration']='PT'+str(30664+delta)+'S';row.update(metadata=encode(raw),duration=raw['contentDetails']['duration'])
    with pytest.raises(HubError,match='SOURCE_IDENTITY_CHANGED'):f.q.observed_identity(c,row,f.q.policy())


def test_revision_window_is_from_original_not_last_accepted_observation(fifo):
    f,c=actual(fifo,ACTUAL[0]);raw=deepcopy(ACTUAL[0]['official_raw'])
    raw['contentDetails']['duration']='PT30669S';write_observation(f,raw);f.q._fresh(c,f.q.policy())
    raw['contentDetails']['duration']='PT30670S';write_observation(f,raw)
    with pytest.raises(HubError,match='SOURCE_IDENTITY_CHANGED'):f.q._fresh(c,f.q.policy())
    assert f.q.candidate(c['candidate_id'])['identity_sha256']==c['identity_sha256']


@pytest.mark.parametrize('field',['identity_sha256','source_metadata_sha256','source_payload'])
def test_original_snapshot_tampering_cannot_use_compatibility_branch(fifo,field):
    f,c=actual(fifo,ACTUAL[0]);changed=deepcopy(c)
    if field=='source_payload':changed[field]['raw']['contentDetails']['duration']='PT30666S'
    else:changed[field]='0'*64
    with pytest.raises(HubError,match='SOURCE_IDENTITY_CHANGED'):f.q._fresh(changed,f.q.policy())


def test_pinned_real_raw_is_not_silently_replaced_and_child_proof_is_bound_to_parent(fifo):
    f,c=actual(fifo,ACTUAL[1]);job,_,service=prepare(f,c);proof=deepcopy(job['result'][PLANNING_SOURCE])
    changed=deepcopy(ACTUAL[1]['official_raw']);changed['snippet']['title']='Later official title'
    write_observation(f,changed)
    assert service.resource_binding(job)['official_raw']==proof['official_raw']
    made=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**15)['created'];download=f.db.claim('video',600,4)
    assert download['job_id']==made[0];bad=deepcopy(download);bad['payload'][PLANNING_SOURCE]['official_raw']['snippet']['title']='unbound'
    with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(bad['payload']),download['job_id']))
    with pytest.raises(HubError):f.q.assert_job_binding(bad)


def test_no_duration_drift_keeps_original_legacy_payload_and_result_shape(fifo):
    f=configure_v4(fifo);raw,_=seed(f,2);c=f.q.register_all_observed()['registered'][0];c=f.q.candidate(c)
    job,calls,_=prepare(f,c)
    assert PLANNING_SOURCE not in job['result'] and calls[0]['official_raw']==raw


def test_lost_real_lease_cannot_pin_planning_source(fifo):
    f,c=actual(fifo,ACTUAL[0]);jid=f.q.enqueue_plan_tick(f.handlers)['created'][0];job=f.db.claim('video',600,4)
    with f.db.transaction() as db:db.execute('UPDATE jobs SET lease_owner=? WHERE job_id=?',('foreign-owner',jid))
    with pytest.raises(HubError):YouTubeWholeReplayPrepare(f.s,f.db)._pin_planning_source(job)
    assert PLANNING_SOURCE not in f.db.get(jid)['result']


def test_stale_registration_gets_real_fresh_response_before_pinning(fifo,monkeypatch):
    f,c=actual(fifo,ACTUAL[1]);old=c['source_payload']['raw']
    write_observation(f,old,observed=time.time()-1000)
    # Queue while fresh, then make the row stale before the real worker starts.
    write_observation(f,old);jid=f.q.enqueue_plan_tick(f.handlers)['created'][0]
    write_observation(f,old,observed=time.time()-1000);_,info=inventory_fixture();calls=[]
    current_raw=ACTUAL[1]['official_raw'];info.update(id=c['source_id'],channel_id=c['channel_id'],duration=38919)
    class HTTP:
        def request(self,*args,**kw):calls.append((args,kw));return {'items':[current_raw]}
    class OwnedInventory:
        def acquire(self,job,binding,*,check):
            check();assert binding['official_raw']==current_raw
            return {'version':VERSION,'binding_sha256':sha(binding),
                'source_plan':plan_whole_replay(binding['official_raw'],info,observed_at=datetime.now(timezone.utc).isoformat(),policy=f.whole),
                'media_downloaded':False,'human_review':False,'publication_authorized':False}
    monkeypatch.setenv('YOUTUBE_API_KEY','owned-fake-not-a-real-key')
    service=YouTubeWholeReplayPrepare(f.s,f.db,http=HTTP(),downloader_factory=lambda _:OwnedInventory())
    assert Worker(f.s,f.db,{PLAN_JOB:service.process}).once('video')
    job=f.db.get(jid);assert job['state']=='completed' and len(calls)==1
    assert job['result'][PLANNING_SOURCE]['official_raw']==current_raw
    assert job['result'][PLANNING_SOURCE]['observed_at']>time.time()-30


def test_capacity_wait_and_maintenance_resume_validate_the_pinned_raw(fifo):
    f,c=actual(fifo,ACTUAL[1]);job,_,_=prepare(f,c)
    out=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=1)
    assert out['created']==[] and f.q.candidate(c['candidate_id'])['state']=='waiting_resource'
    f.clock[0]+=301
    assert f.q.capacity_tick(f.whole,free_bytes=10**15)['ready']==[c['candidate_id']]
    # Exercise the separate expired-hold validator against the same real plan.
    with f.db.transaction() as db:
        db.execute("UPDATE replay_fifo_capacity_v4 SET state='hold' WHERE candidate_id=?",(c['candidate_id'],))
        db.execute("UPDATE replay_fifo_v4 SET state='hold',reason='CAPACITY_WAIT_TIMEOUT' WHERE candidate_id=?",(c['candidate_id'],))
    assert f.q.resume_capacity_hold(c['candidate_id'],f.whole,maintenance_reference='Owned capacity observation',free_bytes=10**15)['state']=='plan_queued'
    assert f.db.get(job['job_id'])==job


def test_fifo_official_refresh_accepts_only_bounded_revision_and_keeps_original_snapshot(fifo,monkeypatch):
    f,c=actual(fifo,ACTUAL[0]);jid=f.q.request_refresh(c['candidate_id']);calls=[]
    class HTTP:
        def request(self,*a,**kw):calls.append(kw);return {'items':[ACTUAL[0]['official_raw']]}
    monkeypatch.setenv('YOUTUBE_API_KEY','owned-fake-not-a-real-key')
    assert Worker(f.s,f.db,{'youtube_replay_refresh_v4':lambda j:f.q.process_refresh(j,HTTP())}).once('video')
    assert f.db.get(jid)['state']=='completed' and len(calls)==1
    latest=f.q.candidate(c['candidate_id'])
    for key in ('source_payload','identity_sha256','source_metadata_sha256','registered_at'):
        assert latest[key]==c[key]


def test_extractor_two_second_tolerance_is_not_expanded(fifo):
    f,c=actual(fifo,ACTUAL[1]);raw=ACTUAL[1]['official_raw'];_,info=inventory_fixture()
    info.update(id=c['source_id'],channel_id=c['channel_id'],duration=38922)
    with pytest.raises(HubError,match='YOUTUBE_NATURAL_SOURCE_CHANGED'):
        plan_whole_replay(raw,info,observed_at=datetime.now(timezone.utc).isoformat(),policy=f.whole)
