import os
import time
from adapters.youtube import YouTubeOfficial
from adapters.youtube_feed import YouTubeFeed
from .db import encode
from .errors import HubError


class YouTubeMonitor:
    def __init__(self, settings, store, http):
        self.settings, self.store = settings, store
        self.feed = YouTubeFeed()
        self.youtube = YouTubeOfficial(http)

    def _poll(self, key, *, error=None, metadata=None, next_check=0):
        now = time.time()
        with self.store.transaction() as db:
            db.execute('''INSERT INTO source_polls VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET checked_at=excluded.checked_at,
                last_success_at=COALESCE(excluded.last_success_at,source_polls.last_success_at),
                error_code=excluded.error_code,metadata=excluded.metadata,next_check_at=excluded.next_check_at''',
                (key, now, None if error else now, error, encode(metadata or {}), next_check))

    def feeds(self, job):
        successes, failures, seen, deferred = 0, [], 0, 0
        retry = None
        folder = self.settings.root / 'media' / job['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        for source in self.settings.config['sources']:
            channel = source['youtube_channel_id']
            from .source_controls import source_paused
            if source_paused(self.settings.root, 'youtube', channel):
                continue
            key = 'youtube-feed:' + channel
            poll = self.store.query('SELECT next_check_at FROM source_polls WHERE source_key=?', (key,))
            if poll and poll[0]['next_check_at'] > time.time():
                deferred += 1
                continue
            try:
                result, raw = self.feed.fetch(channel)
                (folder / (channel + '.xml')).write_bytes(raw)
                now = time.time()
                with self.store.transaction() as db:
                    for video in result['videos']:
                        db.execute('''INSERT INTO youtube_videos(video_id,channel_id,title,published_at,url,state,
                            first_seen_at,last_seen_at,source_api,metadata) VALUES(?,?,?,?,?,'unknown',?,?,?,?)
                            ON CONFLICT(video_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                            title=CASE WHEN last_api_check IS NULL THEN excluded.title ELSE title END''',
                            (video['video_id'],channel,video['title'],video['published_at'],video['url'],now,now,
                             video['source_api'],encode(video)))
                self._poll(key, metadata={'entries':len(result['videos']),'sha256':result['feed_sha256']})
                successes += 1
                seen += len(result['videos'])
            except HubError as exc:
                # A feed's 404 is real unavailability, never evidence that a stream ended.
                delay = 0 if exc.retryable and job['attempts'] < self.settings.config['max_attempts'] else 86400
                self._poll(key, error=exc.code, next_check=time.time() + delay if delay else 0)
                failures.append({'channel_id':channel,'error_code':exc.code})
                self.store.event('review.required', {'code':exc.code,'source':key}, dedup_key=key)
                if exc.retryable:
                    retry = exc
        result = {'status':'partial' if failures else ('deferred' if not successes and deferred else 'ok'),
            'channels_successful':successes,'channels_deferred':deferred,'failures':failures,
            'videos_seen':seen,'live_state_verified':False,'downloads_performed':0}
        (folder / 'feed-result.json').write_text(encode(result), 'utf-8')
        if retry:
            raise retry
        return result

    def observe_video(self, video, expected_channel_id):
        """Persist official API observations and enqueue completed replays idempotently."""
        import re
        from datetime import datetime
        ident = video.get('id', '')
        snippet, live = video.get('snippet', {}), video.get('liveStreamingDetails', {})
        if (not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident) or snippet.get('channelId') != expected_channel_id
                or not snippet.get('title') or not snippet.get('publishedAt')):
            raise HubError('YOUTUBE_VIDEO_IDENTITY_MISMATCH', review=True)
        try:
            timestamps = {key:datetime.fromisoformat(value.replace('Z','+00:00')) for key,value in
                {'publishedAt':snippet['publishedAt'],**{key:live[key] for key in
                ('scheduledStartTime','actualStartTime','actualEndTime') if live.get(key)}}.items()}
            if any(value.tzinfo is None for value in timestamps.values()):
                raise ValueError()
            if live.get('actualEndTime') and (not live.get('actualStartTime') or
                    timestamps['actualEndTime'] < timestamps['actualStartTime']):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HubError('YOUTUBE_LIVE_TIMESTAMPS_INVALID', review=True) from None
        public = video.get('status', {}).get('privacyStatus') == 'public'
        upload_status = video.get('status', {}).get('uploadStatus')
        state = ('unavailable' if not public else
                 'processing' if live.get('actualEndTime') and upload_status in ('uploaded', 'processing') else
                 'ended' if live.get('actualEndTime') else
                 'live' if live.get('actualStartTime') else 'upcoming' if
                 live.get('scheduledStartTime') or snippet.get('liveBroadcastContent') == 'upcoming' else
                 'unknown' if snippet.get('liveBroadcastContent') == 'live' else 'ordinary')
        now = time.time()
        duration = video.get('contentDetails', {}).get('duration')
        url = 'https://www.youtube.com/watch?v=' + ident
        stored_state = 'unknown' if state == 'processing' else state
        with self.store.transaction() as db:
            previous = db.execute('SELECT state,channel_id FROM youtube_videos WHERE video_id=?', (ident,)).fetchone()
            if previous and previous['channel_id'] != expected_channel_id:
                raise HubError('YOUTUBE_VIDEO_IDENTITY_MISMATCH', review=True)
            db.execute('''INSERT INTO youtube_videos(video_id,channel_id,title,published_at,url,state,
                scheduled_start,actual_start,actual_end,duration,first_seen_at,last_seen_at,last_api_check,source_api,metadata)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,published_at=excluded.published_at,state=excluded.state,scheduled_start=excluded.scheduled_start,
                actual_start=excluded.actual_start,actual_end=excluded.actual_end,duration=excluded.duration,
                last_seen_at=excluded.last_seen_at,last_api_check=excluded.last_api_check,
                source_api=excluded.source_api,metadata=excluded.metadata''',
                (ident,expected_channel_id,snippet['title'],snippet['publishedAt'],url,stored_state,
                 live.get('scheduledStartTime'),live.get('actualStartTime'),live.get('actualEndTime'),duration,
                 now,now,now,'youtube-data-v3-official',encode(video)))
            if state in ('upcoming','live','ended') and (not previous or previous['state'] != state):
                self.store._event(db, 'youtube.' + state, {'video_id':ident,'channel_id':expected_channel_id,'url':url})
            elif state == 'processing' and (not previous or previous['state'] != 'unknown'):
                self.store._event(db, 'youtube.processing', {'video_id':ident,'channel_id':expected_channel_id,'url':url})
        if state == 'ended':
            if not duration:
                raise HubError('YOUTUBE_REPLAY_DURATION_UNAVAILABLE', retryable=True)
            payload = {'video_id':ident,'channel_id':expected_channel_id,'title':snippet['title'],
                'published_at':snippet['publishedAt'],'duration':duration,'url':url,
                'source_api':'youtube-data-v3-official','raw':video}
            job_id = self.store.enqueue('youtube',ident,'video_process','video',payload,self.settings.config['policy_version'])
            with self.store.transaction() as db:
                db.execute('UPDATE youtube_videos SET replay_job_id=? WHERE video_id=?', (job_id,ident))
            # A separate policy and job kind enable new downloads; old metadata
            # completions and their replay_job_id retain their original meaning.
            from .youtube_download import YouTubeDownload
            YouTubeDownload(self.settings,self.store).enqueue_if_eligible(payload)
            if self.settings.config.get('youtube_natural_download_enabled',False) is not False:
                from .youtube_download_v3 import YouTubeNaturalPrepare
                YouTubeNaturalPrepare(self.settings,self.store).enqueue_if_eligible(payload)
        return state

    def live(self, job):
        if not os.getenv('YOUTUBE_API_KEY'):
            raise HubError('YOUTUBE_CREDENTIAL_REQUIRED', review=True)
        folder = self.settings.root / 'media' / job['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        counts = {}
        errors = []
        for source in self.settings.config['sources']:
            channel = source['youtube_channel_id']
            from .source_controls import source_paused
            if source_paused(self.settings.root, 'youtube', channel):
                continue
            key = 'youtube-live:' + channel
            tracked = self.store.query('''SELECT video_id FROM youtube_videos WHERE channel_id=?
                AND state IN ('unknown','upcoming','live','unavailable') ORDER BY first_seen_at DESC LIMIT 50''', (channel,))
            try:
                result = self.youtube.observe(source['youtube_handle'],channel,
                    [row['video_id'] for row in tracked],job_id=job['job_id'])
                (folder / (channel + '.json')).write_text(encode(result), 'utf-8')
                for video in result['videos']:
                    state = self.observe_video(video, channel)
                    counts[state] = counts.get(state, 0) + 1
                # An omitted/private/deleted video must never be treated as an ended replay.
                with self.store.transaction() as db:
                    for ident in result['missing_ids']:
                        db.execute('''UPDATE youtube_videos SET state='unavailable',last_api_check=?
                            WHERE video_id=? AND channel_id=?''', (time.time(),ident,channel))
                self._poll(key, metadata={'observed':len(result['videos']),'unavailable':len(result['missing_ids'])})
            except HubError as exc:
                self._poll(key, error=exc.code)
                errors.append(exc)
        if errors:
            raise errors[0]
        return {'states_observed':counts,'source_api':'youtube-data-v3-official','downloads_performed':0}

    def status(self):
        return {'data_api_configured':bool(os.getenv('YOUTUBE_API_KEY')),
            'feed_enabled':self.settings.config.get('youtube_feed_enabled',False),
            'live_monitor_requested':self.settings.config.get('youtube_live_monitor_enabled',False),
            'polls':self.store.query('SELECT * FROM source_polls ORDER BY source_key'),
            'video_states':self.store.query('SELECT state,COUNT(*) count FROM youtube_videos GROUP BY state'),
            'live_or_upcoming':self.store.query('''SELECT video_id,channel_id,title,url,state,scheduled_start,
                actual_start,last_api_check FROM youtube_videos WHERE state IN ('live','upcoming') ORDER BY first_seen_at DESC LIMIT 50''')}
