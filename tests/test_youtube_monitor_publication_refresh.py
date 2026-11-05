"""Owned feed/API observations; no network, download, or historical job edits."""
from copy import deepcopy
import json

import pytest

from adapters.youtube_feed import parse_feed
from services.hub.errors import HubError
from services.hub.youtube_download import YouTubeDownload, CHANNELS
from services.hub.youtube_monitor import YouTubeMonitor

CHANNEL = 'UCaaaaaaaaaaaaaaaaaaaaaa'
OTHER = 'UCbbbbbbbbbbbbbbbbbbbbbb'
VIDEO = '9T5tn1WGlJg'
OLD = '2026-10-31T14:00:12Z'
LATEST = '2026-11-01T01:44:55Z'


def feed(channel):
    entry = (f'<entry><yt:videoId>{VIDEO}</yt:videoId><yt:channelId>{channel}</yt:channelId>'
        f'<title>Owned stream</title><published>{OLD}</published></entry>') if channel == CHANNEL else ''
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
        f'<yt:channelId>{channel}</yt:channelId><title>Owned feed</title>{entry}</feed>').encode()


def official(published=LATEST, channel=CHANNEL, *, ended=True):
    live = {'scheduledStartTime':'2026-10-31T16:00:00Z'}
    if ended:
        live.update(actualStartTime='2026-10-31T16:00:36Z',actualEndTime='2026-10-31T23:09:22Z')
    return {'id':VIDEO,'snippet':{'channelId':channel,'title':'Owned stream','publishedAt':published,
        'liveBroadcastContent':'none' if ended else 'upcoming'},'status':{'privacyStatus':'public'},
        'contentDetails':{'duration':'PT7H8M35S'},'liveStreamingDetails':live}


@pytest.fixture
def observed(runtime):
    settings,store=runtime
    settings.config['sources']=[{'youtube_channel_id':c,'youtube_handle':'@owned-fixture'} for c in (CHANNEL,OTHER)]
    monitor=YouTubeMonitor(settings,store,None)
    class OwnedFeed:
        def fetch(self,channel):
            raw=feed(channel)
            return parse_feed(raw,channel),raw
    monitor.feed=OwnedFeed()
    job=store.enqueue('synthetic_acceptance','publication-refresh','youtube_feed','system',{},'human-review-v1')
    assert monitor.feeds(store.get(job))['videos_seen']==1
    return settings,store,monitor,job


def test_feed_then_ended_official_refreshes_published_at_without_changing_first_seen(observed):
    _,store,monitor,_=observed
    before=store.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO,))[0]
    assert before['published_at']==OLD and before['state']=='unknown'
    assert monitor.observe_video(official(),CHANNEL)=='ended'
    after=store.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO,))[0]
    assert after['published_at']==LATEST and json.loads(after['metadata'])['snippet']['publishedAt']==LATEST
    assert after['first_seen_at']==before['first_seen_at'] and after['last_seen_at']>=before['last_seen_at']
    assert after['channel_id']==CHANNEL and after['source_api']=='youtube-data-v3-official'


def test_repeated_official_change_preserves_historical_payload_and_enqueues_one_download(observed):
    settings,store,monitor,job=observed
    first_seen=store.query('SELECT first_seen_at FROM youtube_videos WHERE video_id=?',(VIDEO,))[0]['first_seen_at']
    assert monitor.observe_video(official(OLD,ended=False),CHANNEL)=='upcoming'
    assert monitor.observe_video(official(OLD),CHANNEL)=='ended'
    history=store.get(store.query("SELECT job_id FROM jobs WHERE job_type='video_process'")[0]['job_id'])
    assert history['payload']['published_at']==OLD
    for _ in range(2):assert monitor.observe_video(official(),CHANNEL)=='ended'
    # A later, older Atom entry cannot replace the latest official publication.
    monitor.feeds(store.get(job))
    row=store.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO,))[0]
    assert row['published_at']==LATEST and row['first_seen_at']==first_seen
    assert store.get(history['job_id'])==history
    assert len(store.query("SELECT * FROM jobs WHERE job_type='video_process'"))==1
    assert len(store.query("SELECT * FROM events WHERE name='youtube.ended'"))==1
    settings.config.update(youtube_download_enabled=True,youtube_download_acceptance_only=True)
    policy={'version':'youtube-download-policy-v1','policy_id':'youtube-download-v1',
        'activation_at':'2026-10-30T00:00:00Z','allowed_channel_ids':sorted(CHANNELS),
        'acceptance_video_ids':[VIDEO],'owner_permission_reference':'Owned synthetic rights only',
        'acquisition_authorization_reference':'Owned fixture consent only','acquisition_mode':'owner_accepted_platform_terms_risk',
        'owner_risk_acceptance_reference':'Owned fixture risk consent only','owner_risk_accepted_at':'2026-10-30T00:00:00Z',
        'max_bytes':3_800_000_000,'max_height':720,'timeout_seconds':21600,'max_new_replays_per_day':4}
    path=settings.root/'config/youtube-download-policy.toml';path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join(k+' = '+json.dumps(v) for k,v in policy.items())+'\n','utf-8')
    # This is exactly the DB->payload construction used by the local queue CLI;
    # the new official observation repairs it, without modifying rows by hand.
    payload={k:row[k] for k in ('video_id','channel_id','title','published_at','duration','url')}
    payload.update(source_api=row['source_api'],raw=json.loads(row['metadata']))
    service=YouTubeDownload(settings,store)
    assert service.enqueue_if_eligible(payload) is None
    download=service.enqueue_acceptance(payload)
    assert service.enqueue_acceptance(deepcopy(payload))==download
    assert len(store.query("SELECT * FROM jobs WHERE job_type='youtube_download'"))==1
    assert store.get(download)['payload']['published_at']==LATEST and store.get(history['job_id'])==history
    assert not store.query('SELECT * FROM publications') and not store.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('expected',[CHANNEL,OTHER])
def test_wrong_or_changed_channel_rejected_before_modifying_existing_observation(observed,expected):
    _,store,monitor,_=observed
    before=store.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO,))[0]
    with pytest.raises(HubError,match='YOUTUBE_VIDEO_IDENTITY_MISMATCH'):
        monitor.observe_video(official(channel=OTHER),expected)
    assert store.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO,))[0]==before
    assert not store.query("SELECT * FROM jobs WHERE job_type IN ('youtube_download','video_process')")
