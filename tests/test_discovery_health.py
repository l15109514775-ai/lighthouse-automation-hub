import json

from services.hub.discovery_health import youtube_discovery_health
from services.hub.maintenance import Maintenance


def configure(runtime):
    settings, store = runtime
    settings.config['youtube_live_monitor_enabled'] = True
    channels = [s['youtube_channel_id'] for s in settings.config['sources']]
    with store.transaction() as db:
        for channel in channels:
            db.execute('INSERT INTO source_polls(source_key,checked_at,last_success_at) VALUES(?,?,?)',
                       ('youtube-live:' + channel, 1000, 1000))
    return settings, store, channels


def pause(settings, channels):
    path = settings.root / 'config/source-controls.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'version': 'source-controls-v1', 'paused_x_handles': [],
        'paused_youtube_channel_ids': channels, 'reason': 'Synthetic owner pause'}))
    path.chmod(0o640)


def test_healthy_downloader_does_not_mask_disabled_discovery(runtime):
    settings, store, _ = configure(runtime)
    settings.config['youtube_live_monitor_enabled'] = False
    with store.transaction() as db: db.execute('INSERT INTO heartbeats VALUES(?,?)', ('worker:replay-acquisition', 1000))
    result = youtube_discovery_health(settings, store, now=1010)
    assert not result['ready'] and result['attention_required']
    assert result['code'] == 'YOUTUBE_DISCOVERY_DISABLED'


def test_disabled_stack_without_active_acquisition_is_not_an_alert(runtime):
    settings, store, _ = configure(runtime)
    settings.config['youtube_live_monitor_enabled'] = False
    result = youtube_discovery_health(settings, store, now=1010)
    assert result['state'] == 'disabled' and not result['attention_required']


def test_owner_paused_channel_does_not_require_a_new_poll(runtime):
    settings, store, channels = configure(runtime)
    pause(settings, channels[:1])
    with store.transaction() as db: db.execute('DELETE FROM source_polls WHERE source_key=?', ('youtube-live:' + channels[0],))
    result = youtube_discovery_health(settings, store, now=1010)
    assert result['ready'] and len(result['channels']) == 1
    assert result['paused_channel_ids'] == channels[:1]


def test_all_sources_paused_is_not_healthy_or_an_outage(runtime):
    settings, store, channels = configure(runtime); pause(settings, channels)
    result = youtube_discovery_health(settings, store, now=9999)
    assert result['state'] == 'owner_paused' and not result['ready'] and not result['attention_required']


def test_expired_or_future_observation_cannot_prove_readiness(runtime):
    settings, store, _ = configure(runtime)
    for now in (999, 2000):
        result = youtube_discovery_health(settings, store, now=now)
        assert result['code'] == 'YOUTUBE_DISCOVERY_OBSERVATION_STALE' and not result['ready']


def test_provider_error_overrides_previous_success_without_leaking_text(runtime):
    settings, store, channels = configure(runtime)
    with store.transaction() as db:
        db.execute('UPDATE source_polls SET error_code=? WHERE source_key=?',
                   ('secret-bearing arbitrary response', 'youtube-live:' + channels[0]))
    result = youtube_discovery_health(settings, store, now=1010)
    assert result['code'] == 'YOUTUBE_DISCOVERY_PROVIDER_ERROR' and not result['ready']
    assert 'secret-bearing' not in json.dumps(result)


def test_invalid_observation_is_json_safe_and_not_ready(runtime):
    settings, store, channels = configure(runtime)
    with store.transaction() as db:
        db.execute('UPDATE source_polls SET last_success_at=? WHERE source_key=?',
                   (float('inf'), 'youtube-live:' + channels[0]))
    result = youtube_discovery_health(settings, store, now=1010)
    assert not result['ready'] and result['channels'][0]['last_success_at'] is None
    json.dumps(result, allow_nan=False)


def test_invalid_pause_configuration_fails_closed(runtime):
    settings, store, _ = configure(runtime)
    pause(settings, [])
    (settings.root / 'config/source-controls.json').write_text('{')
    result = youtube_discovery_health(settings, store, now=1010)
    assert result['code'] == 'SOURCE_CONTROLS_INVALID' and result['attention_required']


def test_maintenance_persists_discovery_alert_and_result(runtime, monkeypatch):
    settings, store, _ = configure(runtime)
    monkeypatch.setattr('services.hub.maintenance.time.time', lambda: 2000)
    monkeypatch.setattr('services.hub.monitoring.host_snapshot', lambda s: {'available': False})
    monkeypatch.setattr('services.hub.events.Events.dispatch', lambda self: [])
    result = Maintenance(settings, store).monitor({})
    assert result['youtube_discovery']['attention_required']
    events = store.query("SELECT data FROM events WHERE name='pipeline.stalled'")
    assert any(json.loads(r['data']).get('code') == 'YOUTUBE_DISCOVERY_OBSERVATION_STALE' for r in events)
