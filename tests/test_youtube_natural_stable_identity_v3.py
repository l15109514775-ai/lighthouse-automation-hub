"""Review regressions: real official-row updates and concurrent discovery paths."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest

from services.hub.errors import HubError
from services.hub.youtube_download import digest
from services.hub.youtube_download_v3 import YouTubeNaturalDownload,PREPARE_JOB_TYPE
from services.hub.youtube_monitor import YouTubeMonitor
from test_youtube_download_v3 import natural,prepare


@pytest.mark.parametrize('change',['title','description','viewers_removed','published_at_representation'])
def test_editorial_official_refresh_preserves_live_download_identity_and_original_payload(natural,change):
    f=natural;parent,result,job=prepare(f);service=YouTubeNaturalDownload(f.s,f.db)
    binding=service._binding(job)[1];before_parent=f.db.get(parent['job_id']);before_job=f.db.get(job['job_id'])
    latest=deepcopy(f.raw)
    if change=='title':latest['snippet']['title']='Owned revised title after the stream ended'
    if change=='description':latest['snippet']['description']='Owned edited public description'
    if change=='viewers_removed':
        # A provider can add/remove a non-identity counter during stabilization.
        latest['liveStreamingDetails']['concurrentViewers']='123'
    if change=='published_at_representation':latest['snippet']['publishedAt']='2026-11-01T01:59:59Z'
    assert YouTubeMonitor(f.s,f.db,None).observe_video(latest,f.payload['channel_id'])=='ended'
    assert service._binding(job)[1]==binding
    service.check_binding(job,binding)
    assert f.db.get(parent['job_id'])==before_parent and f.db.get(job['job_id'])==before_job
    assert len(f.db.query('SELECT * FROM jobs WHERE job_type=?',(PREPARE_JOB_TYPE,)))==1
    observed=service.latest_observation(job['payload'])
    assert observed['metadata_sha256']==digest(latest) and observed['original_metadata_preserved'] is True
    assert binding['metadata_sha256']==digest(f.raw) and binding['official_identity_sha256']==observed['official_identity_sha256']
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_worker_restart_finishes_checkpointed_parent_before_claiming_download(natural):
    from services.hub.worker import Worker
    from services.hub.youtube_download_v3 import YouTubeNaturalPrepare,PREPARE_STATE
    from test_youtube_download_v3 import owned_planner
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    result=YouTubeNaturalPrepare(f.s,f.db,downloader_factory=owned_planner(f)).process(job)
    child=result[PREPARE_STATE]['download_job_id']
    class NoReplan:
        def __init__(self,s):raise AssertionError('Committed plan must survive restart without another extraction')
    worker=Worker(f.s,f.db,{PREPARE_JOB_TYPE:YouTubeNaturalPrepare(f.s,f.db,downloader_factory=NoReplan).process})
    assert worker.once('video') is True
    assert f.db.get(ident)['state']=='completed' and f.db.get(ident)['attempts']==2
    assert f.db.get(child)['state']=='pending' and f.db.get(child)['attempts']==0
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_expired_metadata_cache_is_archived_and_refetched_without_touching_media(natural,monkeypatch):
    from datetime import datetime,timezone,timedelta
    from adapters import ytdlp_natural_v3 as native
    from services.hub.media_processing import atomic_json
    from test_youtube_download_v3 import owned_planner
    f=natural;ident=f.service.enqueue_if_eligible(f.payload);job=f.db.claim('video',600,4)
    binding=f.service._binding(job)[1];planner=native.NaturalReplayPlanner(f.s);area=planner.folder(ident)
    original=owned_planner(f)(f.s).acquire(job,binding,check=lambda:None)
    original['source_plan']['observed_at']=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat().replace('+00:00','Z')
    atomic_json(area/'download-result.json',original);note=area/'owner-note.json';note.write_bytes(b'{"owned":"retain"}')
    calls=[]
    def owned_new_extract(*args):
        calls.append('owned new extraction, no HTTP');return owned_planner(f)(f.s).acquire(job,binding,check=lambda:None)
    monkeypatch.setattr(planner,'_helper',owned_new_extract)
    result=planner.acquire(job,binding,check=lambda:f.service.check_binding(job,binding))
    assert result['source_plan']['observed_at']!=original['source_plan']['observed_at'] and len(calls)==1
    assert (area/'expired-plan-before-attempt-1.json').is_file() and note.read_bytes()==b'{"owned":"retain"}'
    assert not f.db.query('SELECT * FROM api_calls') and not list(area.glob('*.mp4*'))


@pytest.mark.parametrize('change',['start','end','duration','privacy','missing_start','missing_end','channel'])
def test_actual_identity_changes_still_stop_claimed_download(natural,change):
    f=natural;parent,result,job=prepare(f);service=YouTubeNaturalDownload(f.s,f.db);binding=service._binding(job)[1]
    latest=deepcopy(f.raw)
    if change=='start':latest['liveStreamingDetails']['actualStartTime']='2026-11-01T02:00:01Z'
    if change=='end':latest['liveStreamingDetails']['actualEndTime']='2026-11-01T03:00:01Z'
    if change=='duration':latest['contentDetails']['duration']='PT2H'
    if change=='privacy':latest['status']['privacyStatus']='private'
    if change=='missing_start':latest['liveStreamingDetails'].pop('actualStartTime')
    if change=='missing_end':latest['liveStreamingDetails'].pop('actualEndTime')
    if change=='channel':latest['snippet']['channelId']='UCbbbbbbbbbbbbbbbbbbbbbb'
    try:YouTubeMonitor(f.s,f.db,None).observe_video(latest,f.payload['channel_id'])
    except HubError:pass  # The monitor may reject before updating the source.
    if change in ('missing_start','channel'):
        # Its own rejection is already fail closed and leaves the trusted row
        # unchanged; a forged future observation cannot replace that row.
        assert service._binding(job)[1]==binding
    else:
        with pytest.raises(HubError):service.check_binding(job,binding)
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')


def test_long_live_crosses_beijing_0900_and_both_discovery_paths_create_one_task(natural):
    f=natural;raw=deepcopy(f.raw);raw['id']='ownedCross1'
    raw['liveStreamingDetails']={'actualStartTime':'2026-11-01T00:30:00Z'} # Beijing08:30.
    raw['snippet']['liveBroadcastContent']='live';raw['contentDetails']['duration']='PT0S'
    monitor=YouTubeMonitor(f.s,f.db,None)
    assert monitor.observe_video(raw,f.payload['channel_id'])=='live'
    assert not f.db.query('SELECT * FROM jobs WHERE source_id=? AND job_type=?',(raw['id'],PREPARE_JOB_TYPE))
    ended=deepcopy(raw);ended['liveStreamingDetails']['actualEndTime']='2026-11-01T02:00:00Z' # Beijing10:00.
    ended['snippet']['liveBroadcastContent']='none';ended['contentDetails']['duration']='PT1H30M'
    # A five-minute live monitor and daily09:00 scan can race on the same
    # finalized official record. SQLite admission is persistent and exclusive.
    with ThreadPoolExecutor(max_workers=2) as pool:
        states=list(pool.map(lambda _:monitor.observe_video(deepcopy(ended),f.payload['channel_id']),range(2)))
    assert states==['ended','ended']
    rows=f.db.query('SELECT * FROM jobs WHERE source_id=?',(raw['id'],))
    assert len([row for row in rows if row['job_type']==PREPARE_JOB_TYPE])==1
    assert len([row for row in rows if row['job_type']=='video_process'])==1
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')
