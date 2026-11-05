"""Download-only replay jobs, separate from old metadata and publishing policy."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import tomllib
import uuid
from zoneinfo import ZoneInfo

from .db import encode
from .errors import HubError
from .evidence import sha256_file

VERSION = 'youtube-replay-download-v1'
POLICY_VERSION = 'youtube-download-policy-v1'
POLICY_ID = 'youtube-download-v1'
AUTH_POLICY_ID = 'youtube-download-auth-v1'
STATE = 'youtube_download_v1'
CHANNELS = frozenset(('UCaaaaaaaaaaaaaaaaaaaaaa', 'UCbbbbbbbbbbbbbbbbbbbbbb'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError()
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError()
    return parsed.timestamp()


def duration_seconds(value,*,maximum=86400):
    if type(maximum) is not int or not 1<=maximum<=36000*32:
        raise HubError('YOUTUBE_DOWNLOAD_DURATION_INVALID',review=True)
    match = re.fullmatch(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', value or '')
    if not match or not any(match.groups()):
        raise HubError('YOUTUBE_DOWNLOAD_DURATION_INVALID', review=True)
    seconds = sum(float(v or 0) * n for v, n in zip(match.groups(), (86400, 3600, 60, 1)))
    if not math.isfinite(seconds) or not 0 < seconds <= maximum:
        raise HubError('YOUTUBE_DOWNLOAD_DURATION_INVALID', review=True)
    return seconds


class YouTubeDownload:
    def __init__(self, settings, store, *, downloader_factory=None):
        self.settings, self.store = settings, store
        self.downloader_factory = downloader_factory

    def policy(self):
        if self.settings.config.get('youtube_download_enabled') is not True:
            raise HubError('YOUTUBE_DOWNLOAD_DISABLED', review=True)
        file = self.settings.root / 'config/youtube-download-policy.toml'
        try:
            if (file.is_symlink() or not file.is_file() or file.stat().st_size > 16384 or
                    (os.name != 'nt' and file.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH))):
                raise ValueError()
            raw = file.read_bytes()
            value = tomllib.loads(raw.decode('utf-8-sig'))
            if value.get('version') != POLICY_VERSION or value.get('policy_id') not in (POLICY_ID, AUTH_POLICY_ID):
                raise ValueError()
            session_keys = {key for key in value if key.startswith('session_')}
            if value['policy_id'] == AUTH_POLICY_ID:
                if session_keys != {'session_authorization_reference', 'session_authorized_at', 'session_profile'}:
                    raise ValueError()
                session_reference = value.get('session_authorization_reference')
                authorized_at = timestamp(value.get('session_authorized_at'))
                if (value.get('session_profile') != 'youtube' or not isinstance(session_reference, str) or
                        not session_reference.strip() or len(session_reference) > 2048 or
                        not math.isfinite(authorized_at) or authorized_at > time.time()):
                    raise ValueError()
            elif session_keys:
                # Anonymous policy never silently acquires a browser account.
                raise ValueError()
            channels = value.get('allowed_channel_ids')
            configured = [s.get('youtube_channel_id') for s in self.settings.config['sources']]
            if (not isinstance(channels, list) or len(channels) != 2 or set(channels) != CHANNELS or
                    len(configured) != 2 or set(configured) != CHANNELS):
                raise ValueError()
            for key, minimum, maximum in (('max_bytes', 16384, 3_800_000_000), ('max_height', 144, 1080),
                    ('timeout_seconds', 30, 21600), ('max_new_replays_per_day', 1, 4)):
                if type(value.get(key)) is not int or not minimum <= value[key] <= maximum:
                    raise ValueError()
            for key in ('owner_permission_reference', 'acquisition_authorization_reference'):
                if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 2048:
                    raise ValueError()
            if value['max_bytes'] > self.settings.config['max_media_bytes']:
                raise ValueError()
            activation = timestamp(value.get('activation_at'))
            if not math.isfinite(activation):
                raise ValueError()
            acceptance = value.get('acceptance_video_ids', [])
            if (not isinstance(acceptance, list) or len(acceptance) > 1 or
                    any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', v) for v in acceptance)):
                raise ValueError()
            mode = value.get('acquisition_mode')
            if mode == 'platform_authorized':
                reference = value.get('platform_download_permission_reference')
            elif mode == 'owner_accepted_platform_terms_risk':
                reference = value.get('owner_risk_acceptance_reference')
                if timestamp(value.get('owner_risk_accepted_at')) > time.time():
                    raise ValueError()
            else:
                raise ValueError()
            if not isinstance(reference, str) or not reference.strip() or len(reference) > 2048:
                raise ValueError()
            return {**value, '_sha256': hashlib.sha256(raw).hexdigest(), '_activation_epoch': activation}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise HubError('YOUTUBE_DOWNLOAD_POLICY_INVALID', review=True) from None

    def _metadata(self, payload, policy):
        try:
            ident, channel, raw = payload['video_id'], payload['channel_id'], payload['raw']
            if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident) or
                    channel not in policy['allowed_channel_ids'] or payload.get('source_api') != 'youtube-data-v3-official' or
                    payload.get('url') != 'https://www.youtube.com/watch?v=' + ident or raw.get('id') != ident or
                    raw.get('snippet', {}).get('channelId') != channel or
                    raw.get('snippet', {}).get('title') != payload.get('title') or
                    raw.get('snippet', {}).get('publishedAt') != payload.get('published_at') or
                    raw.get('status', {}).get('privacyStatus') != 'public' or
                    raw.get('contentDetails', {}).get('duration') != payload.get('duration')):
                raise ValueError()
            live = raw['liveStreamingDetails']
            ended, started = timestamp(live['actualEndTime']), timestamp(live['actualStartTime'])
            if ended < started or ended > time.time() + 30:
                raise ValueError()
            seconds = duration_seconds(payload['duration'])
            rows = self.store.query('SELECT * FROM youtube_videos WHERE video_id=?', (ident,))
            if (not rows or rows[0]['channel_id'] != channel or rows[0]['state'] != 'ended' or
                    rows[0]['source_api'] != 'youtube-data-v3-official' or rows[0]['duration'] != payload['duration'] or
                    rows[0]['actual_end'] != live['actualEndTime'] or not rows[0]['last_api_check'] or
                    json.loads(rows[0]['metadata']) != raw):
                raise ValueError()
            return {'video_id': ident, 'channel_id': channel, 'metadata_sha256': digest(raw),
                    'duration': seconds, 'actual_end_epoch': ended}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise HubError('YOUTUBE_DOWNLOAD_OFFICIAL_BINDING_REQUIRED', review=True) from None

    def enqueue_if_eligible(self, payload):
        if self.settings.config.get('youtube_download_acceptance_only', False) is True:
            return None
        if self.settings.config.get('youtube_download_enabled') is not True:
            return None
        return self._enqueue(payload, 'automatic')

    def enqueue_acceptance(self, payload):
        return self._enqueue(payload, 'acceptance')

    def _enqueue(self, payload, mode):
        policy = self.policy()
        metadata = self._metadata(payload, policy)
        if mode == 'automatic' and metadata['actual_end_epoch'] < policy['_activation_epoch']:
            return None
        if mode == 'acceptance' and metadata['video_id'] not in policy.get('acceptance_video_ids', []):
            raise HubError('YOUTUBE_DOWNLOAD_ACCEPTANCE_NOT_AUTHORIZED', review=True)
        requested = {**deepcopy(payload), '_download_policy_sha256': policy['_sha256'], '_download_mode': mode}
        now = time.time()
        local = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.store.transaction() as db:
            old = db.execute("SELECT job_id FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type='youtube_download' AND policy_version=?",
                             (metadata['video_id'], policy['policy_id'])).fetchone()
            if old:
                return old['job_id']
            count = db.execute("SELECT COUNT(*) n FROM jobs WHERE source_platform='youtube' AND job_type='youtube_download' AND created_at>=? AND created_at<?",
                               (day.timestamp(), (day + timedelta(days=1)).timestamp())).fetchone()['n']
            if count >= policy['max_new_replays_per_day']:
                return None
            job_id = str(uuid.uuid4())
            db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                VALUES(?,'youtube',?,'youtube_download','video','pending',?,?,?,?)""",
                (job_id, metadata['video_id'], now, now, policy['policy_id'], encode(requested)))
            self.store._event(db, 'youtube.download_queued', {'job_id': job_id, 'video_id': metadata['video_id'],
                                                            'mode': mode, 'publication_authorized': False})
        return job_id

    def _binding(self, job):
        policy = self.policy()
        current = self.store.get(job.get('job_id')) if isinstance(job, dict) else None
        if (not current or current['state'] != 'processing' or current['source_platform'] != 'youtube' or
                current['job_type'] != 'youtube_download' or current['queue'] != 'video' or
                current['policy_version'] != policy['policy_id'] or not job.get('lease_owner') or
                current['lease_owner'] != job['lease_owner'] or (current.get('lease_until') or 0) <= time.time() or
                current['payload'] != job['payload']):
            raise HubError('YOUTUBE_DOWNLOAD_LEASE_OR_INPUT_CHANGED', review=True)
        payload = current['payload']
        metadata = self._metadata(payload, policy)
        mode = payload.get('_download_mode')
        if (current['source_id'] != metadata['video_id'] or payload.get('_download_policy_sha256') != policy['_sha256'] or
                mode not in ('automatic', 'acceptance') or
                (mode == 'automatic' and metadata['actual_end_epoch'] < policy['_activation_epoch']) or
                (mode == 'acceptance' and metadata['video_id'] not in policy.get('acceptance_video_ids', []))):
            raise HubError('YOUTUBE_DOWNLOAD_POLICY_OR_SOURCE_CHANGED', review=True)
        if not 1 <= current['attempts'] <= min(4, self.settings.config['max_attempts']):
            raise HubError('YOUTUBE_DOWNLOAD_RETRY_EXHAUSTED')
        binding = {'version': VERSION, 'job_id': current['job_id'], 'video_id': metadata['video_id'],
            'channel_id': metadata['channel_id'], 'metadata_sha256': metadata['metadata_sha256'],
            'source_payload_sha256': digest(payload), 'duration': metadata['duration'], 'max_bytes': policy['max_bytes'],
            'max_height': policy['max_height'], 'timeout_seconds': policy['timeout_seconds'],
            'policy_sha256': policy['_sha256'], 'request_mode': mode,
            'authenticated_session': policy['policy_id'] == AUTH_POLICY_ID}
        if binding['authenticated_session']:
            binding.update(session_profile='youtube', session_authorization_sha256=policy['_sha256'])
        return current, binding

    def _asset(self, asset, binding):
        try:
            file = Path(asset['file'])
            root = self.settings.root / 'media' / binding['job_id']
            if (not file.is_absolute() or not file.resolve().is_relative_to(root.resolve()) or
                    any(p.is_symlink() for p in (root.parent, root, file, *file.parents)) or not file.is_file() or
                    type(asset['bytes']) is not int or not 0 < asset['bytes'] <= binding['max_bytes'] or
                    file.stat().st_size != asset['bytes'] or not re.fullmatch('[a-f0-9]{64}', asset['sha256']) or
                    sha256_file(file) != asset['sha256'] or type(asset.get('duration')) not in (int, float) or
                    not math.isfinite(asset['duration']) or abs(asset['duration'] - binding['duration']) > 2 or
                    sorted(asset.get('stream_types', {}).values()) != ['audio', 'video']):
                raise ValueError()
            packets = asset.get('packet_evidence', {})
            if set(packets) != {'audio', 'video'} or any(type(v.get('packets')) is not int or v['packets'] < 1 or
                    not isinstance(v.get('chain'), str) or not re.fullmatch('[a-f0-9]{64}', v['chain']) for v in packets.values()):
                raise ValueError()
            return asset
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise HubError('YOUTUBE_DOWNLOAD_ASSET_INTEGRITY_CHANGED', review=True) from None

    def check_binding(self, job, binding):
        """Child/parent polling check; never renew a lease or modify authority."""
        _, actual = self._binding(job)
        if actual != binding:
            raise HubError('YOUTUBE_DOWNLOAD_POLICY_OR_SOURCE_CHANGED', review=True)

    def process(self, job):
        current, binding = self._binding(job)
        previous = current['result'].get(STATE)
        if previous and previous.get('binding') != binding:
            raise HubError('YOUTUBE_DOWNLOAD_POLICY_OR_SOURCE_CHANGED', review=True)

        def check():
            self.check_binding(job, binding)

        if previous and previous.get('state') == 'verified':
            self._asset(previous.get('source_asset'), binding)
            check()
            return current['result']
        state = {'version': VERSION, 'state': 'acquiring', 'binding': binding,
                 'publication_authorized': False, 'human_review': False}
        self.store.checkpoint(job, 'youtube_download_acquiring', {**current['result'], STATE: state})
        factory = self.downloader_factory
        if factory is None:
            from adapters.ytdlp_replay import ReplayDownloader
            factory = ReplayDownloader
        acquired = factory(self.settings).acquire(job, binding, check=check)
        check()
        asset = self._asset(acquired['source_asset'], binding)
        receipt = acquired.get('receipt', {})
        if not isinstance(receipt, dict) or len(encode(receipt).encode()) > 65536:
            raise HubError('YOUTUBE_DOWNLOAD_RECEIPT_INVALID', review=True)
        state.update(state='verified', source_asset=asset, receipt=receipt,
                     verified_at=datetime.now(timezone.utc).isoformat())
        result = {**job['result'], STATE: state, 'disposition': 'replay_download_verified',
                  'download_performed': True, 'publish_performed': False,
                  'publication_authorized': False, 'human_review': False}
        self.store.checkpoint(job, 'youtube_download_verified', result)
        return result
