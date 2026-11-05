from pathlib import Path
import time
import pytest
from adapters.youtube import YouTubeOfficial
from services.hub.video_pipeline import VideoPipeline
from services.hub.publishing import Publishing
from services.hub.errors import HubError, PublishUnknown
from services.hub.evidence import sha256_file
from conftest import enqueue


def test_youtube_completed_public_replays_only(monkeypatch):
    monkeypatch.setenv('YOUTUBE_API_KEY', 'synthetic-only')
    channel_id = 'UC' + 'a' * 22
    class API:
        def request(self, provider, method, url, **kwargs):
            if url.endswith('/channels'):
                assert kwargs['params']['forHandle'] == '@fixture'
                return {'items':[{'id':channel_id,'contentDetails':{'relatedPlaylists':{'uploads':'UUfixture'}}}]}
            if url.endswith('/playlistItems'):
                return {'items':[{'contentDetails':{'videoId':'abcdefghijk'}}]}
            base = {'id':'abcdefghijk','snippet':{'title':'Synthetic replay','publishedAt':'2026-10-30T00:00:00Z','channelId':channel_id},
                    'contentDetails':{'duration':'PT1H'},'status':{'privacyStatus':'public'}}
            return {'items':[{**base,'liveStreamingDetails':{'actualEndTime':'2026-10-30T01:00:00Z'}},
                             {**base,'id':'not-a-replay'}, {**base,'id':'live-now','liveStreamingDetails':{'actualStartTime':'today'}}]}
    result = YouTubeOfficial(API()).replays('@fixture', channel_id)
    assert len(result['replays']) == 1
    assert result['replays'][0]['video_id'] == 'abcdefghijk'


def test_unauthorized_video_never_downloads(runtime):
    settings, store = runtime
    pipeline = VideoPipeline(settings, store, None)
    ident = store.enqueue('youtube','abcdefghijk','video_process','video',
                          {'video_id':'abcdefghijk','url':'https://www.youtube.com/watch?v=abcdefghijk'}, 'human-review-v1')
    result = pipeline.process(store.get(ident))
    assert result['disposition'] == 'metadata_only'
    assert result['download_performed'] is False and result['publish_performed'] is False
    assert not list((settings.root / 'media').rglob('*.mp4'))


def prepare_publish(runtime):
    settings, store = runtime
    settings.dry_run = False
    settings.config.update(auto_publish=True,first_publish_accepted=True,authorized_bilibili_accounts=['synthetic-account'])
    ident = enqueue(store)
    job = store.get(ident)
    file = settings.root / 'media' / 'synthetic.bin'
    file.write_bytes(b'explicit synthetic artifact never sent to any live platform')
    artifact = {'file':str(file),'sha256':sha256_file(file)}
    return job, artifact


def test_dry_run_never_calls_platform(runtime):
    settings, store = runtime
    class Forbidden:
        def account_identity(self): pytest.fail('Live access in DRY_RUN')
    result = Publishing(settings, store, Forbidden()).submit(store.get(enqueue(store)), {}, 'unused', 'image')
    assert result == {'state':'dry_run','sent':False}
    assert not store.query('SELECT * FROM publications')


def test_unknown_publish_is_queried_before_any_resend(runtime):
    settings, store = runtime
    job, artifact = prepare_publish(runtime)
    class Publisher:
        sent, queried = 0, 0
        def account_identity(self): return 'synthetic-account'
        def send(self, key, artifact):
            self.sent += 1
            raise TimeoutError('synthetic lost response')
        def lookup(self, key, external_id=None):
            self.queried += 1
            return {'state':'awaiting_moderation','external_id':'synthetic-resource'}
    adapter = Publisher()
    service = Publishing(settings, store, adapter)
    with pytest.raises(PublishUnknown): service._dispatch(job, artifact, 'synthetic-account', 'image')
    result = Publishing(settings, store, adapter)._dispatch(job, artifact, 'synthetic-account', 'image')
    assert result['state'] == 'awaiting_moderation'
    assert adapter.sent == 1 and adapter.queried == 1
    assert store.query('SELECT external_id FROM publications')[0]['external_id'] == 'synthetic-resource'


def test_not_found_after_timeout_does_not_authorize_duplicate(runtime):
    settings, store = runtime
    job, artifact = prepare_publish(runtime)
    class Publisher:
        sent = 0
        def account_identity(self): return 'synthetic-account'
        def send(self, *args): self.sent += 1; raise TimeoutError()
        def lookup(self, *args): return {'state':'not_found'}
    adapter = Publisher()
    service = Publishing(settings, store, adapter)
    for _ in range(2):
        with pytest.raises(PublishUnknown): service._dispatch(job, artifact, 'synthetic-account', 'image')
    assert adapter.sent == 1


def test_unaccepted_first_publish_stays_blocked(runtime):
    settings, store = runtime
    settings.dry_run = False
    with pytest.raises(HubError, match='FIRST_PUBLISH_ACCEPTANCE_REQUIRED'):
        Publishing(settings, store, None).submit(store.get(enqueue(store)), {}, 'test', 'image')


def test_synthetic_machine_checks_cannot_supply_human_review(runtime):
    settings, store = runtime
    job, artifact = prepare_publish(runtime)
    with pytest.raises(HubError, match='PUBLISH_REVIEW_EVIDENCE_REQUIRED'):
        Publishing(settings, store, None).submit(job, artifact, 'synthetic-account', 'image')
    assert not store.query('SELECT * FROM publication_reviews')
    assert not store.query('SELECT * FROM publications')
