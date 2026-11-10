"""Owner operational pauses, separate from rights and immutable source policy."""
import json
import os
from pathlib import Path
import re

from .errors import HubError


def paused_sources(root):
    root = Path(root)
    path = root / 'config/source-controls.json'
    if not path.exists() and not path.is_symlink():
        return {'x': (), 'youtube': ()}
    try:
        if (path.is_symlink() or any(p.is_symlink() for p in path.parents) or
                not path.is_file() or path.stat().st_size > 16384 or
                (os.name == 'posix' and path.stat().st_mode & 0o022)):
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        if set(value) != {'version', 'paused_x_handles', 'paused_youtube_channel_ids', 'reason'} or value['version'] != 'source-controls-v1':
            raise ValueError()
        result = {}
        for key, field, pattern in [('x', 'paused_x_handles', r'[A-Za-z0-9_]{1,15}'),
                                    ('youtube', 'paused_youtube_channel_ids', r'UC[A-Za-z0-9_-]{22}')]:
            items = value[field]
            if (not isinstance(items, list) or len(items) > 100 or
                    any(not isinstance(x, str) or not re.fullmatch(pattern, x) for x in items) or
                    len(set(items)) != len(items)):
                raise ValueError()
            result[key] = tuple(x.lower() if key == 'x' else x for x in items)
        if not isinstance(value['reason'], str) or len(value['reason']) > 300:
            raise ValueError()
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise HubError('SOURCE_CONTROLS_INVALID', review=True) from None


def source_paused(root, platform, identity):
    identity = identity.lower() if platform == 'x' and isinstance(identity, str) else identity
    return identity in paused_sources(root)[platform]


def _replay_channel(store, job):
    if job['job_type'] == 'youtube_replay_readiness_v4':
        # The observation has a unique queue key. Its plan owns the actual video ID.
        payload = job.get('payload')
        if not isinstance(payload, dict):
            raise HubError('SOURCE_FORWARDING_IDENTITY_REQUIRED', review=True)
        sequence = payload.get('_whole_readiness_sequence')
        video = payload.get('video_id')
        if (not isinstance(video, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video)
                or type(sequence) is not int or not 2 <= sequence <= 96
                or job['source_id'] != f'{video}:readiness:{sequence}'
                or not isinstance(payload.get('_whole_readiness_parent_job_id'), str)
                or not isinstance(payload.get('channel_id'), str)):
            raise HubError('SOURCE_FORWARDING_IDENTITY_REQUIRED', review=True)
        rows = store.query(
            """SELECT v.channel_id FROM jobs p JOIN youtube_videos v ON v.video_id=p.source_id
               WHERE p.job_id=? AND p.source_platform='youtube' AND p.queue='video'
                 AND p.job_type='youtube_replay_plan_v4' AND p.policy_version=?
                 AND p.source_id=? AND v.channel_id=?
                 AND json_extract(p.payload,'$.video_id')=v.video_id
                 AND json_extract(p.payload,'$.channel_id')=v.channel_id""",
            (payload.get('_whole_readiness_parent_job_id'), job['policy_version'], video, payload.get('channel_id')),
        )
    else:
        rows = store.query('SELECT channel_id FROM youtube_videos WHERE video_id=?', (job['source_id'],))
    if not rows:
        raise HubError('SOURCE_FORWARDING_IDENTITY_REQUIRED', review=True)
    return rows[0]['channel_id']


def guard_job(settings, store, job):
    """Pause new processing/publication; never rewrite an existing job or receipt.

    Discovery handles its configured sources separately. Lookup/reconciliation
    tasks retain their own handlers and may observe an already dispatched result.
    The primary post author matters; quoted/replied-to authors are not pause keys.
    """
    pauses = paused_sources(settings.root)
    kind = job['job_type']
    if kind in ('tweet_process', 'tweet_machine_publish'):
        source = job
        if kind == 'tweet_machine_publish':
            parent = job.get('payload', {}).get('parent_job_id')
            source = store.get(parent) if isinstance(parent, str) else None
        if not pauses['x']:
            return
        try:
            handle = source['payload']['author']['username']
            if not isinstance(handle, str): raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise HubError('SOURCE_FORWARDING_IDENTITY_REQUIRED', review=True) from None
        if handle.lower() in pauses['x']:
            raise HubError('SOURCE_FORWARDING_PAUSED', review=True)
    if job['source_platform'] == 'youtube' and pauses['youtube'] and kind != 'replay_part_lookup_v5':
        if _replay_channel(store, job) in pauses['youtube']:
            raise HubError('SOURCE_FORWARDING_PAUSED', review=True)
