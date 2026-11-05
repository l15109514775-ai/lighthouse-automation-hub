"""Documented YouTube Atom topic; metadata only, never a video downloader."""
from datetime import datetime
import hashlib
import http.client
import re
import ssl
import xml.etree.ElementTree as ET
from .safe_https import open_request
from services.hub.errors import HubError

NS = {'a':'http://www.w3.org/2005/Atom', 'yt':'http://www.youtube.com/xml/schemas/2015'}
MAX_FEED_BYTES = 524288


def parse_feed(data, expected_channel_id):
    if (not isinstance(data, bytes) or len(data) > MAX_FEED_BYTES or
            not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', expected_channel_id)):
        raise HubError('YOUTUBE_FEED_INPUT_INVALID', review=True)
    try:
        xml = data.decode('utf-8-sig')
        if '\x00' in xml or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
            raise HubError('YOUTUBE_FEED_XML_DECLARATION_FORBIDDEN', review=True)
        root = ET.fromstring(xml)
        if (root.tag != '{' + NS['a'] + '}feed' or
                root.findtext('yt:channelId', namespaces=NS) != expected_channel_id):
            raise HubError('YOUTUBE_CHANNEL_IDENTITY_MISMATCH', review=True)
        videos = {}
        entries = root.findall('a:entry', NS)
        if len(entries) > 100:
            raise HubError('YOUTUBE_FEED_ENTRY_LIMIT', review=True)
        for entry in entries:
            video_id = entry.findtext('yt:videoId', namespaces=NS) or ''
            channel_id = entry.findtext('yt:channelId', namespaces=NS)
            title = entry.findtext('a:title', namespaces=NS) or ''
            published = entry.findtext('a:published', namespaces=NS) or ''
            instant = datetime.fromisoformat(published.replace('Z', '+00:00'))
            if (not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) or channel_id != expected_channel_id
                    or not title or len(title) > 1000 or instant.tzinfo is None):
                raise HubError('YOUTUBE_FEED_ENTRY_INVALID', review=True)
            videos[video_id] = {'video_id':video_id,'channel_id':channel_id,'title':title,
                'published_at':published,'url':'https://www.youtube.com/watch?v=' + video_id,
                'source_api':'youtube-official-atom','state':'unknown'}
        return {'channel_id':expected_channel_id,'title':root.findtext('a:title',namespaces=NS),
                'videos':list(videos.values()),'feed_sha256':hashlib.sha256(data).hexdigest()}
    except (ET.ParseError, ValueError, TypeError, UnicodeError):
        raise HubError('YOUTUBE_FEED_INVALID_XML_OR_DATE', review=True) from None


class YouTubeFeed:
    def fetch(self, channel_id):
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
            raise HubError('INVALID_YOUTUBE_CHANNEL_ID', review=True)
        connection = None
        try:
            url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel_id
            connection, response = open_request(url, ['www.youtube.com'],
                headers={'User-Agent':'LighthouseAutomationHub/0.2','Accept':'application/atom+xml'})
            if response.status == 404:
                raise HubError('YOUTUBE_FEED_NOT_AVAILABLE', review=True)
            if response.status == 429:
                raise HubError('YOUTUBE_FEED_RATE_LIMITED', retryable=True, retry_after=600)
            if response.status >= 500:
                raise HubError('YOUTUBE_FEED_SERVER_ERROR', retryable=True)
            if response.status != 200:
                raise HubError('YOUTUBE_FEED_ACCESS_REJECTED', review=True)
            data = response.read(MAX_FEED_BYTES + 1)
            return parse_feed(data, channel_id), data
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise HubError('YOUTUBE_FEED_NETWORK_ERROR', retryable=True) from None
        finally:
            if connection:
                connection.close()
