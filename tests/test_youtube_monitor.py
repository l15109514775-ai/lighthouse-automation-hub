import pytest
from adapters.youtube import YouTubeOfficial
from adapters.youtube_feed import parse_feed
from scheduler.service import DurableScheduler
from services.hub.db import Store
from services.hub.errors import HubError
from services.hub.youtube_monitor import YouTubeMonitor

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'abcdefghijk'


def atom(channel=CHANNEL, video=VIDEO):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
    <yt:channelId>{channel}</yt:channelId><title>Synthetic channel</title>
    <entry><yt:videoId>{video}</yt:videoId><yt:channelId>{channel}</yt:channelId>
    <title>Synthetic video</title><published>2026-10-30T00:00:00Z</published>
    <link rel="alternate" href="https://untrusted.example.invalid/"/></entry></feed>'''.encode()


def video(live=None, privacy='public', channel=CHANNEL):
    return {'id':VIDEO,'snippet':{'channelId':channel,'title':'Synthetic stream',
            'publishedAt':'2026-10-30T00:00:00Z','liveBroadcastContent':'none'},
            'status':{'privacyStatus':privacy},'contentDetails':{'duration':'PT1H'},
            'liveStreamingDetails':live or {}}


def job(store, kind='youtube_feed'):
    ident = store.enqueue('synthetic_acceptance','youtube-monitor',kind,'system',{},'human-review-v1')
    return store.get(ident)


def test_atom_metadata_does_not_assert_live_or_follow_entry_links():
    result = parse_feed(atom(), CHANNEL)
    assert result['videos'][0]['state'] == 'unknown'
    assert result['videos'][0]['url'] == 'https://www.youtube.com/watch?v=' + VIDEO
    assert len(result['feed_sha256']) == 64


@pytest.mark.parametrize('data', [b'<!DOCTYPE feed [<!ENTITY x "boom">]><feed/>',
    '<!DOCTYPE feed [<!ENTITY x "boom">]><feed/>'.encode('utf-16'),b'<not-xml',b'x'*524289])
def test_untrusted_xml_is_rejected(data):
    with pytest.raises(HubError):
        parse_feed(data, CHANNEL)


def test_atom_wrong_channel_cannot_populate_monitor(runtime):
    with pytest.raises(HubError,match='IDENTITY_MISMATCH'):
        parse_feed(atom('UC'+'b'*22), CHANNEL)
    settings,store=runtime
    assert not store.query('SELECT * FROM youtube_videos')


def test_live_lifecycle_and_restart_enqueue_one_replay(runtime):
    settings,store=runtime
    monitor=YouTubeMonitor(settings,store,None)
    schedule={'scheduledStartTime':'2026-10-30T01:00:00Z'}
    started={**schedule,'actualStartTime':'2026-10-30T01:01:00Z'}
    ended={**started,'actualEndTime':'2026-10-30T02:01:00Z'}
    assert monitor.observe_video(video(schedule),CHANNEL)=='upcoming'
    assert monitor.observe_video(video(started),CHANNEL)=='live'
    assert not store.query("SELECT * FROM jobs WHERE job_type='video_process'")
    assert monitor.observe_video(video(ended),CHANNEL)=='ended'
    restarted=YouTubeMonitor(settings,Store(settings.db_path),None)
    restarted.observe_video(video(ended),CHANNEL)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='video_process'"))==1
    names=[row['name'] for row in store.query('SELECT name FROM events')]
    assert names==['youtube.upcoming','youtube.live','youtube.ended']


def test_ordinary_private_and_invalid_end_do_not_queue_replays(runtime):
    settings,store=runtime
    monitor=YouTubeMonitor(settings,store,None)
    assert monitor.observe_video(video(),CHANNEL)=='ordinary'
    assert monitor.observe_video(video({'actualStartTime':'2026-10-30T00:00:00Z',
        'actualEndTime':'2026-10-30T01:00:00Z'},privacy='private'),CHANNEL)=='unavailable'
    with pytest.raises(HubError,match='TIMESTAMPS_INVALID'):
        monitor.observe_video(video({'actualEndTime':'2026-10-30T01:00:00Z'}),CHANNEL)
    assert not store.query("SELECT * FROM jobs WHERE job_type='video_process'")


def test_feed_repeat_preserves_verified_live_state(runtime):
    settings,store=runtime
    settings.config['sources']=[{'youtube_channel_id':CHANNEL}]
    monitor=YouTubeMonitor(settings,store,None)
    monitor.feed=type('Feed',(),{'fetch':lambda _,channel:(parse_feed(atom(),CHANNEL),atom())})()
    monitor.feeds(job(store))
    monitor.observe_video(video({'actualStartTime':'2026-10-30T01:00:00Z'}),CHANNEL)
    YouTubeMonitor(settings,store,None).observe_video(video({'actualStartTime':'2026-10-30T01:00:00Z'}),CHANNEL)
    monitor.feeds(job(store))
    row=store.query('SELECT * FROM youtube_videos')[0]
    assert row['state']=='live' and row['source_api']=='youtube-data-v3-official'
    assert len(store.query('SELECT * FROM youtube_videos'))==1


def test_feed_404_preserves_error_and_defers_repeated_poll(runtime):
    settings,store=runtime
    settings.config['sources']=[{'youtube_channel_id':CHANNEL}]
    calls=[]
    class Missing:
        def fetch(self,channel):
            calls.append(channel)
            raise HubError('YOUTUBE_FEED_NOT_AVAILABLE',review=True)
    monitor=YouTubeMonitor(settings,store,None)
    monitor.feed=Missing()
    assert monitor.feeds(job(store))['status']=='partial'
    assert monitor.feeds(job(store))['status']=='deferred'
    assert len(calls)==1
    assert store.query('SELECT * FROM source_polls')[0]['last_success_at'] is None
    assert store.query('SELECT * FROM source_polls')[0]['error_code']=='YOUTUBE_FEED_NOT_AVAILABLE'
    assert not store.query("SELECT * FROM jobs WHERE job_type='video_process'")


def test_missing_api_result_never_means_stream_ended(runtime,monkeypatch):
    monkeypatch.setenv('YOUTUBE_API_KEY','synthetic-only')
    settings,store=runtime
    settings.config['sources']=[{'youtube_channel_id':CHANNEL,'youtube_handle':'@fixture'}]
    monitor=YouTubeMonitor(settings,store,None)
    monitor.observe_video(video({'actualStartTime':'2026-10-30T01:00:00Z'}),CHANNEL)
    monitor.youtube=type('API',(),{'observe':lambda *a,**k:{'videos':[],'missing_ids':[VIDEO]}})()
    monitor.live(job(store,'youtube_live_monitor'))
    assert store.query('SELECT state FROM youtube_videos')[0]['state']=='unavailable'
    assert not store.query("SELECT * FROM jobs WHERE job_type='video_process'")


def test_official_monitor_checks_identity_and_tracks_old_live_ids(monkeypatch):
    monkeypatch.setenv('YOUTUBE_API_KEY','synthetic-only')
    seen=[]
    class API:
        def request(self,provider,method,url,**kwargs):
            seen.append(url)
            if url.endswith('/channels'):
                return {'items':[{'id':CHANNEL,'contentDetails':{'relatedPlaylists':{'uploads':'UUfixture'}}}]}
            if url.endswith('/playlistItems'):
                return {'items':[]}
            assert kwargs['params']['id']==VIDEO
            return {'items':[video({'actualStartTime':'2026-10-30T01:00:00Z'})]}
    result=YouTubeOfficial(API()).observe('@fixture',CHANNEL,[VIDEO])
    assert len(result['videos'])==1 and result['missing_ids']==[]
    assert all('/search' not in url for url in seen)
    with pytest.raises(HubError,match='IDENTITY_MISMATCH'):
        YouTubeOfficial(API()).observe('@fixture','UC'+'b'*22)


def test_live_scheduler_requires_key_and_coalesces_without_dot(runtime,monkeypatch):
    settings,store=runtime
    monkeypatch.delenv('YOUTUBE_API_KEY',raising=False)
    scheduler=DurableScheduler(settings,store)
    scheduler.tick(1_793_404_000)
    assert not store.query("SELECT * FROM jobs WHERE job_type='youtube_live_monitor'")
    monkeypatch.setenv('YOUTUBE_API_KEY','synthetic-only')
    scheduler.tick(1_793_404_001)
    DurableScheduler(settings,Store(settings.db_path)).tick(1_793_405_001)
    assert len(store.query("SELECT * FROM jobs WHERE job_type='youtube_live_monitor'"))==1
    assert len(store.query("SELECT * FROM jobs WHERE job_type='youtube_feed'"))==1


def test_feed_network_retries_are_finite_and_do_not_skip_other_channel(runtime):
    from services.hub.worker import Worker
    settings,store=runtime
    other='UC'+'b'*22
    settings.config['sources']=[{'youtube_channel_id':CHANNEL},{'youtube_channel_id':other}]
    attempts=[]
    class Offline:
        def fetch(self,channel):
            attempts.append(channel)
            raise HubError('YOUTUBE_FEED_NETWORK_ERROR',retryable=True)
    monitor=YouTubeMonitor(settings,store,None)
    monitor.feed=Offline()
    ident=job(store)['job_id']
    worker=Worker(settings,store,{'youtube_feed':monitor.feeds})
    for _ in range(settings.config['max_attempts']):
        with store.transaction() as db:
            db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(ident,))
        worker.once('system')
    assert store.get(ident)['state']=='failed'
    assert attempts.count(CHANNEL)==4 and attempts.count(other)==4
    assert store.query('SELECT COUNT(*) n FROM source_polls')[0]['n']==2


def test_existing_video_cannot_change_channel(runtime):
    settings,store=runtime
    monitor=YouTubeMonitor(settings,store,None)
    monitor.observe_video(video(),CHANNEL)
    other='UC'+'b'*22
    with pytest.raises(HubError,match='IDENTITY_MISMATCH'):
        monitor.observe_video(video(channel=other),other)
    assert store.query('SELECT channel_id FROM youtube_videos')[0]['channel_id']==CHANNEL
