import os
import re
from services.hub.errors import HubError


class YouTubeOfficial:
    def __init__(self, client):
        self.client = client

    def observe(self, handle, expected_channel_id, tracked_ids=(), job_id=None):
        """Live lifecycle polling without consuming the separate search.list quota."""
        key = os.getenv('YOUTUBE_API_KEY')
        if not key:
            raise HubError('YOUTUBE_CREDENTIAL_REQUIRED', review=True)
        if (not re.fullmatch(r'@[A-Za-z0-9_.-]+', handle) or
                not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', expected_channel_id) or len(tracked_ids) > 50 or
                any(not re.fullmatch(r'[A-Za-z0-9_-]{11}', value) for value in tracked_ids)):
            raise HubError('INVALID_YOUTUBE_MONITOR_INPUT', review=True)
        def get(resource, **params):
            return self.client.request('youtube', 'GET', 'https://www.googleapis.com/youtube/v3/' + resource,
                params={'key':key, **params}, job_id=job_id)
        channels = get('channels', part='snippet,contentDetails', forHandle=handle).get('items', [])
        if len(channels) != 1 or channels[0]['id'] != expected_channel_id:
            raise HubError('YOUTUBE_CHANNEL_IDENTITY_MISMATCH', review=True)
        uploads = channels[0]['contentDetails']['relatedPlaylists']['uploads']
        listing = get('playlistItems', part='contentDetails', playlistId=uploads, maxResults=50)
        ids = set(tracked_ids)
        ids.update(item['contentDetails']['videoId'] for item in listing.get('items', []))
        if len(ids) > 100 or any(not re.fullmatch(r'[A-Za-z0-9_-]{11}', value) for value in ids):
            raise HubError('YOUTUBE_MONITOR_BOUND_REACHED', review=True)
        found = {}
        ordered = sorted(ids)
        for start in range(0, len(ordered), 50):
            result = get('videos', part='snippet,contentDetails,liveStreamingDetails,status',
                         id=','.join(ordered[start:start + 50]))
            for video in result.get('items', []):
                if (video.get('id') not in ids or video.get('snippet', {}).get('channelId') != expected_channel_id):
                    raise HubError('YOUTUBE_VIDEO_IDENTITY_MISMATCH', review=True)
                found[video['id']] = video
        return {'videos':list(found.values()),'missing_ids':sorted(ids - found.keys()),
                'source_api':'youtube-data-v3-official','channel':channels[0]}

    def replays(self, handle, expected_channel_id, job_id=None):
        key = os.getenv('YOUTUBE_API_KEY')
        if not key:
            raise HubError('YOUTUBE_CREDENTIAL_REQUIRED', review=True)
        if not re.fullmatch(r'@[A-Za-z0-9_.-]+', handle):
            raise HubError('INVALID_YOUTUBE_HANDLE', review=True)
        def get(resource, **params):
            return self.client.request('youtube', 'GET', 'https://www.googleapis.com/youtube/v3/' + resource,
                                       params={'key':key, **params}, job_id=job_id)
        response = get('channels', part='snippet,contentDetails', forHandle=handle)
        items = response.get('items', [])
        if len(items) != 1 or items[0]['id'] != expected_channel_id:
            raise HubError('YOUTUBE_CHANNEL_IDENTITY_MISMATCH', review=True)
        channel = items[0]
        uploads = channel['contentDetails']['relatedPlaylists']['uploads']
        result, pages = [], []
        params = {'part':'contentDetails', 'playlistId':uploads, 'maxResults':50}
        for _ in range(5):
            listing = get('playlistItems', **params)
            pages.append(listing)
            ids = [item['contentDetails']['videoId'] for item in listing.get('items', [])]
            if ids:
                videos = get('videos', part='snippet,contentDetails,liveStreamingDetails,status', id=','.join(ids))
                for video in videos.get('items', []):
                    # An ordinary video, upcoming live or currently live is not a completed replay.
                    if (video.get('liveStreamingDetails', {}).get('actualEndTime')
                            and video.get('status', {}).get('privacyStatus') == 'public'
                            and video.get('snippet', {}).get('channelId') == expected_channel_id):
                        result.append({'video_id':video['id'], 'title':video['snippet']['title'],
                            'published_at':video['snippet']['publishedAt'], 'duration':video['contentDetails']['duration'],
                            'channel_id':expected_channel_id, 'url':'https://www.youtube.com/watch?v=' + video['id'],
                            'source_api':'youtube-data-v3-official', 'raw':video})
            if not listing.get('nextPageToken'):
                return {'replays':result,'channel':channel,'pages':pages,'catchup_complete':True}
            params['pageToken'] = listing['nextPageToken']
        return {'replays':result,'channel':channel,'pages':pages,'catchup_complete':False}
