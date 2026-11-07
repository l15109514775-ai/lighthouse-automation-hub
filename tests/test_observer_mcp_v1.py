"""Snapshot-only observer, including a real independent MCP stdio session."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import logging
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp import types
from mcp.shared.exceptions import McpError
import pytest

from services.hub import observer_mcp_v1 as observer
from test_observer_snapshot_v1 import owned, collect, NOW

JOB = '4ca65e35-8a45-4746-b6c4-13de860409a8'
OLD_JOB = 'eefce1b8-228a-4ef6-a0a4-840d07a7b60d'
WAITING_JOB = '5ba8a447-38a8-434b-95dc-7353a46d4018'
FAIL_JOB = '7f6e3d8a-1111-4222-9333-444455556666'
SECRET = 'owned-sensitive-sentinel-that-must-not-be-returned'


def snapshot(now):
    job = {'job_id': JOB, 'source_platform': 'youtube', 'source_id': 'exampleVid7',
           'job_type': 'youtube_replay_download_v4', 'queue': 'video', 'state': 'processing',
           'stage': 'youtube_whole_acquiring', 'attempts': 1, 'error_code': None,
           'created_at': now-30, 'updated_at': now-1, 'external_publish_id': None,
           'is_historical': False, 'is_current': True,
           'source_url': 'https://www.youtube.com/watch?v=exampleVid7', 'published_url': None,
           'progress': {'phase': 'download', 'format_id': '299', 'percent_scope': 'current_track',
                        'downloaded_bytes': 20, 'total_bytes': 100, 'percent': 20.0,
                        'observed_at': now-1, 'fresh': True, 'reason': None}}
    old = deepcopy(job)
    old.update(job_id=OLD_JOB, state='awaiting_review', is_historical=True, is_current=False,
               job_type='youtube_replay_download_source_revision_history_v1',
               error_code='YOUTUBE_WHOLE_FRESH_INVENTORY_CHANGED')
    old['progress'].update(downloaded_bytes=95, percent=95.0)
    return {'version': observer.VERSION, 'generated_at': now, 'read_only': True, 'provider_requests': 0,
            'health': {'database': 'ok', 'status': 'ready', 'stale_services': []},
            'system': {'disk_free_bytes': 10**10, 'disk_total_bytes': 90*10**9,
                       'memory_total_bytes': 4*10**9, 'memory_available_bytes': 2*10**9, 'load_1m': 0.1},
            'containers': {'available': True, 'checked_at': now, 'items': [
                {'name': 'lighthouse-video-worker-1', 'state': 'running', 'health': 'healthy', 'exit_code': 0, 'restart_count': 0}], 'reason': None},
            'pipelines': [{'name': 'video', 'paused': False}], 'jobs': [job, old],
            'replays': [{'candidate_id': '9064b1bf-74dd-4094-9460-e699ec6cd7cc', 'source_id': 'exampleVid7',
                        'title': 'Owned replay', 'state': 'download_queued', 'reason': None,
                        'plan_job_id': '16cea2cb-2592-5225-9978-1b4e710ca1f5', 'download_job_id': JOB,
                        'publication_job_id': None, 'updated_at': now,
                        'source_url': job['source_url'], 'published_urls': []}],
            'failures': [old],
            'daily_report': {'date': '2026-11-03', 'timezone': 'Asia/Shanghai',
                             'job_counts': [{'state': 'processing', 'count': 1}],
                             'job_counts_scope': 'current_jobs_in_bounded_snapshot',
                             'api_usage': {'daily': [{'provider': 'x', 'calls': 2, 'quota_units': 2,
                                                      'reserved_upper_bound_usd': 0.01}], 'monthly': []},
                             'estimated': True, 'charges_confirmed': False},
            'backup': {'configured': True, 'status': 'last_run_failed', 'last_verified_at': now-1000,
                       'last_verified_age_seconds': 1000.0, 'last_error_at': now-100,
                       'error_code': 'OWNED_FAILURE', 'reason': None}}


def waiting_and_failure_snapshot(now):
    value = snapshot(now)
    waiting = deepcopy(value['jobs'][0])
    waiting.update(job_id=WAITING_JOB, source_id='exampleVid1',
                   job_type='youtube_replay_plan_v4', state='awaiting_review',
                   error_code='REPLAY_FIFO_SOURCE_IDENTITY_CHANGED',
                   normal_wait=True, attention='waiting_source_reconciliation',
                   is_current=True, is_historical=False,
                   source_url='https://www.youtube.com/watch?v=exampleVid1')
    failure = deepcopy(value['jobs'][0])
    failure.update(job_id=FAIL_JOB, state='failed', error_code='OWNED_REAL_FAILURE',
                   normal_wait=False, attention='error', is_current=True, is_historical=False)
    value['jobs'] += [waiting, failure]
    value['waiting'] = [waiting]
    # Deliberately include the normal wait in failures too: list_failures must
    # still exclude it, so a future collector regression cannot leak it.
    value['failures'] = [failure, waiting]
    value['replays'] = [{
        'candidate_id': '765c14a8-b863-49e9-aebe-59acd6da3f24', 'source_id': 'exampleVid1',
        'title': 'Owned waiting replay', 'state': 'hold',
        'reason': 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED', 'plan_job_id': WAITING_JOB,
        'download_job_id': None, 'publication_job_id': None, 'refresh_job_id': None,
        'updated_at': now, 'source_url': 'https://www.youtube.com/watch?v=exampleVid1',
        'published_urls': []},
        # Same hold/reason shape, but the linked job is not a normal wait: the
        # projection must not turn every hold into waiting.
        {'candidate_id': '9064b1bf-74dd-4094-9460-e699ec6cd7cc', 'source_id': 'exampleVid7',
         'title': 'Owned unrelated hold', 'state': 'hold',
         'reason': 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED', 'plan_job_id': JOB,
         'download_job_id': None, 'publication_job_id': None, 'refresh_job_id': None,
         'updated_at': now, 'source_url': 'https://www.youtube.com/watch?v=exampleVid7',
         'published_urls': []}]
    return value


def write_snapshot(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def data(result):
    assert not result.isError
    return result.structuredContent if result.structuredContent is not None else json.loads(result.content[0].text)


def test_real_sdk_stdio_has_exact_read_tools_no_maintenance_or_database_initialization(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = write_snapshot(tmp_path/'snapshot.json', snapshot(time.time()))
    before = path.read_bytes(); forbidden = tmp_path/'must-not-create-hub-root'

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'services.hub.observer_mcp_v1'],
            env={**os.environ, 'HUB_OBSERVER_SNAPSHOT': str(path), 'HUB_ROOT': str(forbidden),
                 'HUB_CONFIG': str(forbidden/'config.toml'), 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {'get_system_status', 'list_jobs', 'get_job_details', 'list_failures', 'get_daily_report'}
                assert all(tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False and
                           tool.annotations.openWorldHint is False for tool in listed.tools)
                status = data(await session.call_tool('get_system_status', {}))
                assert status['availability'] == 'current' and status['backup']['status'] == 'last_run_failed'
                assert status['waiting'] == []
                assert status['replays'][0]['normal_wait'] is False and status['replays'][0]['attention'] is None
                jobs = data(await session.call_tool('list_jobs', {}))
                assert [job['job_id'] for job in jobs['jobs']] == [JOB]
                assert jobs['jobs'][0]['progress']['percent'] == 20.0
                assert jobs['jobs'][0]['normal_wait'] is False and jobs['jobs'][0]['attention'] is None
                history = data(await session.call_tool('get_job_details', {'job_id': OLD_JOB}))
                assert history['job']['is_historical'] is True and history['job']['progress']['percent'] is None
                assert len(data(await session.call_tool('list_jobs', {'current_only': False}))['jobs']) == 2
                assert data(await session.call_tool('list_failures', {}))['failures'] == []
                assert data(await session.call_tool('get_daily_report', {}))['daily_report']['charges_confirmed'] is False
                for tool in ('retry_job', 'pause_pipeline', 'resume_pipeline', 'shell'):
                    assert (await session.call_tool(tool, {'job_id': JOB})).isError
                for arguments in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'state': 'invalid'}, {'current_only': 'true'}):
                    assert (await session.call_tool('list_jobs', arguments)).isError
                assert (await session.call_tool('get_job_details', {'job_id': '../../private.json'})).isError
                missing = data(await session.call_tool('get_job_details', {'job_id': '00000000-0000-0000-0000-000000000000'}))
                assert missing['job'] is None and missing['reason'] == 'NOT_IN_CURRENT_SNAPSHOT'
                path.write_text('{"version":"broken"}', encoding='utf-8')
                unavailable = data(await session.call_tool('get_system_status', {}))
                assert unavailable['availability'] == 'unavailable' and 'system' not in unavailable
                path.write_bytes(before)
    asyncio.run(exercise())
    assert path.read_bytes() == before and not forbidden.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['snapshot.json']


def test_unknown_fields_and_signed_urls_never_cross_read_boundary(tmp_path):
    value = snapshot(time.time()); value['credentials'] = {'secret': SECRET}
    value['health']['raw_error'] = SECRET; value['system']['mount_path'] = SECRET
    value['containers']['items'][0]['env'] = SECRET
    value['backup']['receipt'] = {'secret': SECRET}; value['daily_report']['api_usage']['daily'][0]['key'] = SECRET
    for job in value['jobs'] + value['failures']:
        job.update(payload={'secret': SECRET}, result={'secret': SECRET}, lease_owner=SECRET, path=SECRET)
        job['progress'].update(source_plan={'secret': SECRET}, raw_headers={'secret': SECRET})
        job['published_url'] = 'https://www.bilibili.com/video/BV15o4M6nEiC/?token='+SECRET
    value['replays'][0]['published_urls'] = ['https://www.bilibili.com/video/BV15o4M6nEiC/', 'https://cdn.example.test/?secret='+SECRET]
    path = write_snapshot(tmp_path/'snapshot.json', value)
    meta, body = observer.SnapshotReader(path).load()
    assert meta['availability'] == 'current' and SECRET not in json.dumps(body)
    assert body['replays'][0]['published_urls'] == ['https://www.bilibili.com/video/BV15o4M6nEiC/']
    assert body['jobs'][0]['published_url'] is None


@pytest.mark.parametrize('change', ['stale', 'future', 'missing', 'duplicate_key', 'oversize', 'nonfinite', 'duplicates', 'wrong_version', 'wrong_type', 'symlink'])
def test_invalid_missing_or_stale_snapshots_never_return_current_data(tmp_path, monkeypatch, change):
    now = time.time(); value = snapshot(now); path = tmp_path/'snapshot.json'
    if change == 'stale': value['generated_at'] = now-121
    if change == 'future': value['generated_at'] = now+60
    if change == 'nonfinite': value['generated_at'] = float('nan')
    if change == 'duplicates': value['jobs'].append(deepcopy(value['jobs'][0]))
    if change == 'wrong_version': value['version'] = 'untrusted-version'
    if change == 'wrong_type': value['jobs'][0]['attempts'] = '1'
    if change != 'missing': write_snapshot(path, value)
    if change == 'duplicate_key': path.write_text('{"version":"first","version":"second"}', encoding='utf-8')
    if change == 'oversize': path.write_bytes(b' '*(observer.MAX_BYTES+1))
    if change == 'symlink':
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda self: self == path or original(self))
    meta, body = observer.SnapshotReader(path, clock=lambda: now).load()
    assert body is None and meta['availability'] == ('stale' if change == 'stale' else 'unavailable')
    if change == 'stale': assert meta['snapshot_generated_at'] == now-121


@pytest.mark.parametrize('change', ['old_progress', 'future_progress', 'historical', 'upload', 'over_total', 'zero_total', 'untrusted_percent', 'wrong_phase', 'wrong_scope'])
def test_progress_is_current_track_download_only_and_recomputed(tmp_path, change):
    now = time.time(); value = snapshot(now); job = value['jobs'][0]; progress = job['progress']
    if change == 'old_progress': progress['observed_at'] = now-121
    elif change == 'future_progress': progress['observed_at'] = now+60
    elif change == 'historical': job['is_historical'] = True
    elif change == 'upload': job['job_type'] = 'replay_part_publish_v5_p01'
    elif change == 'over_total': progress['downloaded_bytes'] = 101
    elif change == 'zero_total': progress['total_bytes'] = 0
    elif change == 'untrusted_percent': progress['percent'] = 99.0
    elif change == 'wrong_phase': progress['phase'] = 'upload'
    elif change == 'wrong_scope': progress['percent_scope'] = None
    path = write_snapshot(tmp_path/'snapshot.json', value)
    meta, body = observer.SnapshotReader(path, clock=lambda: now).load()
    assert meta['availability'] == 'current'
    actual = body['jobs'][0]['progress']
    if change == 'untrusted_percent': assert actual['percent'] == 20.0
    else: assert actual['percent'] is None and actual['downloaded_bytes'] is None and actual['fresh'] is False


def test_atomic_snapshot_replacement_is_seen_without_any_source_write(tmp_path):
    now = time.time(); path = write_snapshot(tmp_path/'snapshot.json', snapshot(now)); reader = observer.SnapshotReader(path, clock=lambda: now)
    first = path.read_bytes(); assert reader.load()[1]['jobs'][0]['progress']['downloaded_bytes'] == 20
    updated = snapshot(now); updated['jobs'][0]['progress']['downloaded_bytes'] = 30
    replacement = write_snapshot(tmp_path/'replacement.json', updated); replacement.replace(path)
    changed = path.read_bytes(); assert first != changed
    assert reader.load()[1]['jobs'][0]['progress']['downloaded_bytes'] == 30 and path.read_bytes() == changed


def test_actual_collector_contract_crosses_mcp_projection_without_raw_source_data(owned, tmp_path):
    owned['db'].execute('UPDATE replay_fifo_v4 SET refresh_job_id=?', (owned['planner'],))
    snapshot = collect(owned)
    path = write_snapshot(tmp_path/'snapshot.json', snapshot)
    meta, body = observer.SnapshotReader(path, clock=lambda: NOW).load()
    assert meta['availability'] == 'current'
    assert {job['job_id'] for job in body['jobs']} == {owned[k] for k in ('old', 'current', 'planner', 'legacy')}
    current = next(job for job in body['jobs'] if job['job_id'] == owned['current'])
    assert current['progress']['percent'] == 50.0 and current['progress']['percent_scope'] == 'current_track'
    assert body['failures'] == [] and body['backup']['status'] == 'unknown'
    assert body['replays'][0]['refresh_job_id'] == owned['planner']
    assert 'secret-value-never-export' not in json.dumps(body)


@pytest.mark.parametrize('kind', ['request', 'notification'])
def test_sdk_validation_filter_removes_embedded_input_and_exception_metadata(kind):
    record = logging.LogRecord('root', logging.WARNING, '/opt/venv/lib/python3.12/site-packages/mcp/shared/session.py',
        383, 'Failed to validate '+kind+': '+SECRET+' %s', (SECRET,),
        (ValueError, ValueError(SECRET), None), '_receive_loop', SECRET)
    record.message = record.exc_text = SECRET
    assert observer.SDKValidationLogFilter().filter(record) is True
    assert record.msg == 'MCP_'+kind.upper()+'_VALIDATION_REJECTED'
    assert record.args == () and record.exc_info is record.exc_text is record.stack_info is None
    assert SECRET not in repr(record.__dict__)
    assert logging.Formatter('%(levelname)s:%(message)s').format(record) == 'WARNING:'+record.msg


@pytest.mark.parametrize('change', ['caller', 'path', 'name', 'error', 'info', 'other_warning', 'near_prefix'])
def test_sdk_validation_filter_preserves_other_diagnostics_exactly(change):
    record = logging.LogRecord('root', logging.WARNING, '/opt/venv/lib/python3.12/site-packages/mcp/shared/session.py',
        383, 'Failed to validate request: owned diagnostic %s', ('retained',), None, '_receive_loop')
    if change == 'caller': record.funcName = 'other_function'
    elif change == 'path': record.pathname = '/owned/application/session.py'
    elif change == 'name': record.name = 'owned.application'
    elif change == 'error': record.levelno = logging.ERROR
    elif change == 'info': record.levelno = logging.INFO
    elif change == 'other_warning': record.msg = 'Response ID %s cannot be normalized'
    elif change == 'near_prefix': record.msg = 'Failed to validate request-like owned diagnostic %s'
    before = dict(record.__dict__)
    assert observer.SDKValidationLogFilter().filter(record) is True
    assert record.__dict__ == before


def test_sdk_validation_filter_install_is_idempotent_and_keeps_unrelated_warning(caplog, monkeypatch):
    logger = logging.getLogger(); monkeypatch.setattr(logger, 'filters', [])
    observer.install_sdk_validation_log_filter(); observer.install_sdk_validation_log_filter()
    assert len(logger.filters) == 1 and isinstance(logger.filters[0], observer.SDKValidationLogFilter)
    level = logger.level
    with caplog.at_level(logging.WARNING):
        logging.warning('OWNED_OTHER_WARNING %s', 'retained')
    assert logger.level == level and 'OWNED_OTHER_WARNING retained' in caplog.text


def test_translation_resolution_crosses_read_boundary_and_strips_signed_urls(tmp_path):
    now = time.time(); value = snapshot(now)
    base = {'job_id': '5931b8d3-e510-43af-b8af-cc5e81c7d3c5', 'source_platform': 'machine_model',
            'source_id': 'b4081cb3-ce9d-4552-b513-817f53c2c9d4:post', 'job_type': 'machine_translation_node',
            'queue': 'tweet', 'state': 'failed', 'stage': 'model_quality_rejected', 'attempts': 0,
            'error_code': 'MACHINE_TRANSLATION_CHECK_FAILED', 'created_at': now-60, 'updated_at': now-40,
            'external_publish_id': None, 'is_historical': False, 'is_current': True,
            'source_url': None, 'published_url': None,
            'progress': {'phase': 'model_quality_rejected', 'format_id': None, 'percent_scope': None,
                         'downloaded_bytes': None, 'total_bytes': None, 'percent': None,
                         'observed_at': None, 'fresh': False, 'reason': 'no_measured_progress'},
            'resolution': {'kind': 'superseded_by_revision',
                           'revision_job_id': '66c7d882-b3e2-4986-b950-a66f12186615',
                           'revision_state': 'completed', 'revision_stage': 'model_receipts_verified',
                           'revision_error_code': None, 'revision_updated_at': now-30,
                           'parent_job_id': 'b4081cb3-ce9d-4552-b513-817f53c2c9d4',
                           'parent_state': 'completed',
                           'published_url': 'https://t.bilibili.com/1266459648005767233',
                           'supersession_complete': True}}
    value['jobs'].append(base)
    path = write_snapshot(tmp_path/'snapshot.json', value)
    meta, body = observer.SnapshotReader(path, clock=lambda: now).load()
    assert meta['availability'] == 'current'
    item = next(job for job in body['jobs'] if job['job_id'] == base['job_id'])
    assert item['resolution'] == base['resolution']
    # A signed/non-canonical URL in the resolution is stripped, never returned.
    base['resolution'] = {**base['resolution'], 'published_url': 'https://t.bilibili.com/1266459648005767233?token=' + SECRET}
    value['jobs'][-1] = base
    path = write_snapshot(tmp_path/'snapshot.json', value)
    _, body = observer.SnapshotReader(path, clock=lambda: now).load()
    item = next(job for job in body['jobs'] if job['job_id'] == base['job_id'])
    assert item['resolution']['published_url'] is None and SECRET not in json.dumps(body)


def test_invalid_resolution_shape_never_crosses_read_boundary(tmp_path):
    now = time.time(); value = snapshot(now)
    job = deepcopy(value['jobs'][0]); job['resolution'] = {'kind': 'superseded_by_revision',
        'revision_job_id': SECRET, 'revision_state': 'completed', 'revision_updated_at': now-1,
        'parent_job_id': 'b4081cb3-ce9d-4552-b513-817f53c2c9d4', 'parent_state': 'completed',
        'supersession_complete': True}
    job['job_id'] = '7f6e3d8a-1111-4222-9333-444455556666'
    value['jobs'].append(job)
    path = write_snapshot(tmp_path/'snapshot.json', value)
    meta, body = observer.SnapshotReader(path, clock=lambda: now).load()
    assert body is None and meta['availability'] == 'unavailable' and SECRET not in str(meta)


def test_waiting_job_and_replay_attention_cross_real_mcp_and_failures_stay_separate(tmp_path):
    now = time.time()
    path = write_snapshot(tmp_path/'snapshot.json', waiting_and_failure_snapshot(now))
    root = Path(__file__).resolve().parents[1]

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'services.hub.observer_mcp_v1'],
            env={**os.environ, 'HUB_OBSERVER_SNAPSHOT': str(path), 'PYTHONPATH': str(root),
                 'PYTHONDONTWRITEBYTECODE': '1'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                status = data(await session.call_tool('get_system_status', {}))
                assert [job['job_id'] for job in status['waiting']] == [WAITING_JOB]
                assert status['waiting'][0]['normal_wait'] is True
                assert status['waiting'][0]['attention'] == 'waiting_source_reconciliation'
                replay = status['replays'][0]
                assert replay['plan_job_id'] == WAITING_JOB
                assert replay['normal_wait'] is True
                assert replay['attention'] == 'waiting_source_reconciliation'
                unrelated = status['replays'][1]
                assert unrelated['state'] == 'hold'
                assert unrelated['reason'] == 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED'
                assert unrelated['normal_wait'] is False and unrelated['attention'] is None
                jobs = data(await session.call_tool('list_jobs', {}))
                waiting = next(job for job in jobs['jobs'] if job['job_id'] == WAITING_JOB)
                failure = next(job for job in jobs['jobs'] if job['job_id'] == FAIL_JOB)
                assert waiting['normal_wait'] is True and waiting['attention'] == 'waiting_source_reconciliation'
                assert failure['normal_wait'] is False and failure['attention'] == 'error'
                details = data(await session.call_tool('get_job_details', {'job_id': WAITING_JOB}))
                assert details['job']['normal_wait'] is True and details['job']['attention'] == 'waiting_source_reconciliation'
                failures = data(await session.call_tool('list_failures', {}))
                assert [job['job_id'] for job in failures['failures']] == [FAIL_JOB]
                assert failures['matching_in_snapshot'] == 1

    asyncio.run(exercise())


def test_legacy_snapshot_without_waiting_fields_defaults_and_remains_usable(tmp_path):
    now = time.time()
    path = write_snapshot(tmp_path/'snapshot.json', snapshot(now))
    meta, body = observer.SnapshotReader(path, clock=lambda: now).load()
    assert meta['availability'] == 'current'
    assert body['waiting'] == []
    assert all(job['normal_wait'] is False and job['attention'] is None for job in body['jobs'])
    assert all(replay['normal_wait'] is False and replay['attention'] is None for replay in body['replays'])


def test_real_stdio_rejects_unknown_protocol_without_logging_inputs_and_still_serves_tools(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = write_snapshot(tmp_path/'snapshot.json', snapshot(time.time()))
    stderr = tmp_path/'observer-stderr.log'
    marker = 'owned-log-sentinel'
    # Prove the unfiltered SDK failure really carries the supplied sentinel;
    # the stderr check must not pass merely because input was never included.
    with pytest.raises(observer.ValidationError) as original:
        types.ClientRequest.model_validate({'method': 'server/discover', 'params': {'secret': marker}})
    assert marker in str(original.value)

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'services.hub.observer_mcp_v1'],
            env={**os.environ, 'HUB_OBSERVER_SNAPSHOT': str(path), 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'})
        with stderr.open('w', encoding='utf-8') as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with pytest.raises(McpError) as rejected:
                        await session.send_request(types.Request[dict, str](
                            method='server/discover', params={'secret': marker}), types.EmptyResult)
                    assert rejected.value.error.code == types.INVALID_PARAMS
                    assert rejected.value.error.message == 'Invalid request parameters' and rejected.value.error.data == ''
                    await session.send_notification(types.Notification[dict, str](
                        method='notifications/owned-unknown', params={'secret': marker}))
                    tools = await session.list_tools()
                    assert len(tools.tools) == 5
                    assert data(await session.call_tool('get_system_status', {}))['availability'] == 'current'

    asyncio.run(exercise())
    logs = stderr.read_text(encoding='utf-8')
    assert 'MCP_REQUEST_VALIDATION_REJECTED' in logs and 'MCP_NOTIFICATION_VALIDATION_REJECTED' in logs
    assert marker not in logs and 'input_value=' not in logs
