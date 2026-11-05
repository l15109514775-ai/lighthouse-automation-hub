"""Real FIFO leases and FFmpeg; the identified acquisition double copies owned tracks."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib,json,os,shutil,time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters import ytdlp_highest_v2 as engine
from adapters.ytdlp_whole_v4 import WholeReplayDownloader,folder,budget_limits,select_from_info
from services.hub.budget import Budget
from services.hub.db import encode
from services.hub.errors import HubError,ResourceDeferred
from services.hub.worker import Worker
from services.hub.replay_fifo_v4 import PLAN_JOB,DOWNLOAD_JOB,REFRESH_JOB
from services.hub.replay_multipart_v4 import digest,plan_whole_replay,DEFAULT_POLICY as WHOLE_POLICY
from services.hub.youtube_download_v4 import (YouTubeWholeReplayPrepare,YouTubeWholeReplayDownload,VERSION,STATE,
    POLICY_FILE,PLAN_POLICY_FILE,POLICY_VERSION,WholeResourceDeferredV4,transfer_plan,validate_transfer_plan,resource_callbacks)
from test_replay_fifo_v4 import fifo,seed
from test_ytdlp_highest_v2 import tracks
from test_replay_multipart_v4 import evidence


def toml(value):
    return '\n'.join(key+' = '+(str(v).lower() if type(v) is bool else json.dumps(v,ensure_ascii=False)) for key,v in value.items())+'\n'


def configure_v4(f):
    f.s.config.update(youtube_whole_replay_download_enabled=True,youtube_natural_download_enabled=False)
    refs={k:'Owned synthetic scope reference; no real account authorization' for k in ('owner_permission_reference',
        'acquisition_authorization_reference','quality_authorization_reference','owner_risk_acceptance_reference','session_authorization_reference')}
    p={'version':POLICY_VERSION,'enabled':True,'source_scope':'all_eligible_ended_since_operating_cutoff',
        'planning_policy_sha256':digest(f.whole),'session_profile':'youtube','acquisition_mode':'owner_accepted_platform_terms_risk',
        'allowed_channel_ids':f.p['allowed_channel_ids'],'operating_not_before':f.whole['source_not_before'],
        'daily_http_requests':6000,'monthly_http_requests':150000,'timeout_seconds':300,
        'owner_risk_accepted_at':'2026-11-01T00:00:00Z','session_authorized_at':'2026-11-01T00:00:00Z',**refs}
    (f.s.root/'config'/POLICY_FILE).write_text(toml(p),encoding='utf-8');(f.s.root/'config'/POLICY_FILE).chmod(0o644)
    (f.s.root/'config'/PLAN_POLICY_FILE).write_text(toml(f.whole),encoding='utf-8');(f.s.root/'config'/PLAN_POLICY_FILE).chmod(0o644)
    return f


@pytest.fixture
def whole(fifo,tracks,monkeypatch):
    f=configure_v4(fifo)
    raw,info=seed(f,1);start=datetime.fromisoformat(raw['liveStreamingDetails']['actualStartTime'])
    raw['contentDetails']['duration']='PT1S';raw['liveStreamingDetails']['actualEndTime']=(start+timedelta(seconds=1)).isoformat();info['duration']=1
    info['formats']=[info['formats'][1],info['formats'][-1]]
    for fmt,source in zip(info['formats'],tracks):fmt['filesize']=source.stat().st_size
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET metadata=?,duration=?,actual_end=? WHERE video_id=?',
        (encode(raw),raw['contentDetails']['duration'],raw['liveStreamingDetails']['actualEndTime'],raw['id']))
    f.q.register_all_observed();f.raw=raw;f.info=info;f.inputs=tracks;f.calls=[]
    class OwnedPlanner:
        def acquire(self,job,binding,*,check):
            check();value=plan_whole_replay(raw,info,observed_at=datetime.now(timezone.utc).isoformat(),policy=f.whole)
            return {'version':VERSION,'binding_sha256':digest(binding),'source_plan':value,'media_downloaded':False,
                'human_review':False,'publication_authorized':False,'owned_inventory_only':True}
    f.prepare=YouTubeWholeReplayPrepare(f.s,f.db,downloader_factory=lambda _:OwnedPlanner())
    queued=f.q.enqueue_plan_tick(f.handlers);assert Worker(f.s,f.db,{PLAN_JOB:f.prepare.process}).once('video')
    f.plan_job=f.db.get(queued['created'][0]);assert f.plan_job['state']=='completed'
    made=f.q.enqueue_download_tick(f.handlers,f.whole,free_bytes=10**12);assert len(made['created'])==1
    f.job=f.db.claim('video',180,4);assert f.job['job_id']==made['created'][0]
    downloader=WholeReplayDownloader(f.s)
    def owned_helper(job,binding,area,ticket,output,check):
        check();records=[]
        for source,identity in zip(f.inputs,binding['source_plan']['selected_formats']):
            target=area/('stream-'+identity['format_id']+'.'+identity['ext']);shutil.copyfile(source,target)
            records.append({**identity,'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
        result={'binding_sha256':digest(binding),'tracks':records,'source_plan':binding['source_plan'],
            'selected_formats':binding['source_plan']['selected_formats'],'http':{'requests':0,'actual_youtube_download':False,'owned_copy_only':True}}
        f.calls.append('owned_track_copy_not_youtube');engine.atomic_json(output,result);return result
    monkeypatch.setattr(downloader,'_helper',owned_helper);f.downloader=downloader
    f.service=YouTubeWholeReplayDownload(f.s,f.db,downloader_factory=lambda _:downloader)
    return f


def test_actual_owned_merge_packet_proof_and_parts_reach_verified_without_platform_publish(whole):
    f=whole;original=[p.read_bytes() for p in f.inputs]
    result=f.service.process(f.job);state=result[STATE]
    assert state['state']=='parts_verified' and state['receipt']['source_to_merged_packet_coverage_verified'] is True
    assert state['receipt']['source_stream_config']['video']['avg_frame_rate']=='60/1'
    assert Path(state['source_asset']['file']).parent.name=='youtube-whole-v4'
    assert state['parts']['parts'] and state['publishable'] is result['publish_performed'] is False
    assert f.calls==['owned_track_copy_not_youtube'] and [p.read_bytes() for p in f.inputs]==original
    assert not f.db.query('SELECT * FROM publications') and not f.db.query('SELECT * FROM api_calls')
    assert f.service.process(f.job)==result and f.calls==['owned_track_copy_not_youtube']


def test_new_transfer_plan_math_is_rebuilt_and_old_v3_caps_not_reintroduced(whole):
    f=whole;value=f.job['payload']['whole_replay_plan'];transfer=transfer_plan(value)
    assert validate_transfer_plan(transfer)==transfer
    assert transfer['required_working_bytes']==value['whole_budget']['required_working_bytes']
    transfer['required_working_bytes']-=1
    with pytest.raises(HubError):validate_transfer_plan(transfer)


def test_stale_official_state_makes_one_real_transport_call_and_preserves_original_job_payload(whole,monkeypatch):
    f=whole;before=deepcopy(f.job['payload']);old=time.time()-1000
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET last_api_check=? WHERE video_id=?',(old,f.raw['id']))
    raw=deepcopy(f.raw);raw['snippet']['title']='Owned changed public title, same immutable source';calls=[]
    class HTTP:
        def request(self,provider,method,url,**kw):calls.append((provider,method,url));assert kw['params']['id']==raw['id'];return {'items':[raw]}
    monkeypatch.setenv('YOUTUBE_API_KEY','owned-fixture-key-never-real')
    f.service.http=HTTP();_,binding=f.service._binding(f.job);started=time.time();ref=f.service.fresh_observation(f.job,binding)
    assert len(calls)==1 and started<=ref['observed_at']<=time.time()
    assert f.db.get(f.job['job_id'])['payload']==before and f.db.query('SELECT last_api_check FROM youtube_videos')[0]['last_api_check']>old
    assert json.loads(Path(ref['file']).read_bytes())['new_api_request'] is True


def test_changed_official_channel_stops_without_timestamp_fabrication_or_acquisition(whole,monkeypatch):
    f=whole;old=time.time()-1000
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET last_api_check=? WHERE video_id=?',(old,f.raw['id']))
    changed=deepcopy(f.raw);changed['snippet']['channelId']='UCwrongchannel00000000000'
    class HTTP:
        def request(self,*a,**kw):return {'items':[changed]}
    f.service.http=HTTP();monkeypatch.setenv('YOUTUBE_API_KEY','owned-fixture-key-never-real')
    with pytest.raises(HubError):f.service.process(f.job)
    assert f.calls==[] and f.db.query('SELECT last_api_check FROM youtube_videos')[0]['last_api_check']==old


def test_media_start_rejects_an_official_observation_that_expired_during_extraction(whole):
    f=whole;_,binding=f.service._binding(f.job);old=time.time()-301
    with f.db.transaction() as db:db.execute('UPDATE youtube_videos SET last_api_check=? WHERE video_id=?',(old,f.raw['id']))
    # The long-transfer lease remains valid; its initial media-start proof does not.
    f.service.check_binding(f.job,binding)
    with pytest.raises(HubError,match='FRESH_SOURCE_REQUIRED') as caught:f.service.check_media_start(f.job,binding)
    assert caught.value.retryable is True and f.calls==[]
    assert f.db.query('SELECT last_api_check FROM youtube_videos')[0]['last_api_check']==old


def test_large_whole_inventory_selector_uses_exact_highest_tracks_above_old14gb_cap():
    raw,info,observed,now=evidence();policy={**deepcopy(WHOLE_POLICY),'enabled':True}
    for f in info['formats']:f['url']='https://owned.googlevideo.com/videoplayback?owned=fixture'
    value=plan_whole_replay(raw,info,observed_at=observed,now=now,policy=policy)
    binding={'official_raw':raw,'planning_policy':policy,'whole_replay_plan':value,'source_plan':transfer_plan(value)}
    selected=select_from_info(info,binding)
    assert sum(f['filesize'] for f in selected)==26_000_000_000 and [f['format_id'] for f in selected]==['ownedVideo','ownedAudio']
    assert binding['source_plan']['redirect_resume_request_allowance']==binding['source_plan']['media_requests_plan']
    assert binding['source_plan']['max_http_requests']==2*binding['source_plan']['media_requests_plan']+64
    assert binding['source_plan']['max_http_requests']>2048 and binding['source_plan']['required_working_bytes']>64_000_000_000


def test_missing_session_scope_or_disabled_v4_does_not_touch_media(whole):
    f=whole;f.s.config['youtube_whole_replay_download_enabled']=False
    with pytest.raises(HubError,match='DISABLED'):f.service.process(f.job)
    assert f.calls==[]


def test_new_resource_exception_accepts_whole_budget_without_weakening_legacy_64gb_cap():
    with pytest.raises(ValueError):ResourceDeferred(resource='disk',required_bytes=70_000_000_000,binding_sha256='a'*64)
    value=WholeResourceDeferredV4(required_free_bytes=70_000_000_000,binding_sha256='a'*64)
    assert isinstance(value,ResourceDeferred) and value.required_bytes==70_000_000_000


def test_http_preflight_preserves_shared_ledger_and_has_explicit_new_finite_cap(whole):
    f=whole;_,binding=f.service._binding(f.job);limits=budget_limits(binding)
    budget=Budget(f.db,limits);call=budget.reserve('youtube_download_http',f.job['job_id'],units=50);budget.finish(call,'done')
    before=f.db.query('SELECT * FROM api_calls')
    with pytest.raises(HubError,match='API_BUDGET_EXCEEDED'):engine.preflight_budget(f.db,binding,6001,limits_function=budget_limits,request_cap=20000)
    assert f.db.query('SELECT * FROM api_calls')==before
    with pytest.raises(HubError,match='BUDGET_INVALID'):engine.preflight_budget(f.db,binding,3000,limits_function=budget_limits)


def test_same_job_wait_keeps_partials_attempt_and_binding_then_resumes_via_explicit_callback(whole,monkeypatch):
    f=whole;_,binding=f.service._binding(f.job);area=folder(f.s,f.job['job_id']);partial=area/'stream-owned60.mp4.part';partial.write_bytes(b'Owned partial retained')
    reference=hashlib.sha256(partial.read_bytes()).hexdigest()
    class NoSpace:
        def acquire(self,*a,**kw):raise HubError('DISK_LOW',review=True)
    f.service.downloader_factory=lambda _:NoSpace()
    with pytest.raises(WholeResourceDeferredV4) as result:f.service.process(f.job)
    resource_callbacks(f.s,f.db)[DOWNLOAD_JOB](f.job,result.value)
    held=f.db.get(f.job['job_id']);assert held['state']=='retry_scheduled' and held['attempts']==1 and held['lease_owner'] is None
    assert f.service.resource_binding_sha(held)==digest(binding)
    assert hashlib.sha256(partial.read_bytes()).hexdigest()==reference
    f.clock[0]=time.time()+301
    assert f.q.resource_tick({DOWNLOAD_JOB:f.service.resource_binding_sha},free_bytes=10**12)['resumed']==[held['job_id']]
    import services.hub.db as database
    monkeypatch.setattr(database,'time',SimpleNamespace(time=lambda:f.clock[0]))
    resumed=f.db.claim('video',180,4);assert resumed['job_id']==held['job_id'] and resumed['attempts']==2
    assert hashlib.sha256(partial.read_bytes()).hexdigest()==reference
