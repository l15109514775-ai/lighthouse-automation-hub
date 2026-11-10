"""Read-only official discovery evidence, independent of downloader liveness."""
import math
import re
import time

from .errors import HubError
from .source_controls import paused_sources


def youtube_discovery_health(settings, store, *, now=None):
    now = time.time() if now is None else now
    result = {'version': 'youtube-discovery-health-v1', 'ready': False,
              'attention_required': False, 'channels': [], 'paused_channel_ids': []}

    def finish(state, code=None, attention=False):
        return {**result, 'state': state, 'code': code, 'attention_required': attention,
                'ready': state == 'ready'}

    try:
        cfg = settings.config
        channels = [s['youtube_channel_id'] for s in cfg['sources']]
        if (not channels or len(set(channels)) != len(channels) or
                any(not isinstance(x, str) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', x) for x in channels)):
            return finish('invalid', 'YOUTUBE_DISCOVERY_SOURCE_CONFIG_INVALID', True)
        paused = set(paused_sources(settings.root)['youtube'])
        result['paused_channel_ids'] = [x for x in channels if x in paused]
        channels = [x for x in channels if x not in paused]
        if not channels:
            return finish('owner_paused')
        enabled = cfg.get('youtube_live_monitor_enabled', False)
        interval = cfg.get('youtube_live_poll_minutes', 5)
        if type(enabled) is not bool or type(interval) is not int or not 1 <= interval <= 1440:
            return finish('invalid', 'YOUTUBE_DISCOVERY_CONFIG_INVALID', True)
        if not enabled:
            beats = store.query("SELECT at FROM heartbeats WHERE name='worker:replay-acquisition'")
            expected = any(type(r['at']) in (int, float) and math.isfinite(r['at']) and
                           0 <= now - r['at'] <= 150 for r in beats)
            return finish('disabled', 'YOUTUBE_DISCOVERY_DISABLED' if expected else None, expected)
        window = interval * 120 + 60
        polls = {r['source_key']: r for r in store.query(
            'SELECT source_key,checked_at,last_success_at,error_code FROM source_polls')}
        for channel in channels:
            row = polls.get('youtube-live:' + channel)
            stamp = row['last_success_at'] if row else None
            stamp = stamp if type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0 else None
            age = now - stamp if stamp is not None else None
            error = row['error_code'] if row else None
            if error is not None and (not isinstance(error, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,127}', error)):
                error = 'YOUTUBE_DISCOVERY_RECORDED_ERROR'
            state = 'blocked' if error else 'fresh' if age is not None and 0 <= age < window else 'unverified'
            result['channels'].append({'channel_id': channel, 'state': state,
                'last_success_at': stamp, 'error_code': error})
        result['freshness_window_seconds'] = window
        if any(row['state'] == 'blocked' for row in result['channels']):
            return finish('blocked', 'YOUTUBE_DISCOVERY_PROVIDER_ERROR', True)
        if any(row['state'] != 'fresh' for row in result['channels']):
            return finish('unverified', 'YOUTUBE_DISCOVERY_OBSERVATION_STALE', True)
        return finish('ready')
    except HubError as exc:
        return finish('invalid', exc.code, True)
    except (KeyError, TypeError, ValueError):
        return finish('invalid', 'YOUTUBE_DISCOVERY_CONFIG_INVALID', True)
