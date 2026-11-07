"""The health check must see discovery-complete tweets that never got ingested."""
import importlib.util
import json
import time
from pathlib import Path

from services.hub.db import Store


def health_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/ops/tweet_publish_health_v1.py'
    spec = importlib.util.spec_from_file_location('owned_tweet_publish_health_v1', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deferred_ingest_reports_stuck_official_capture(tmp_path):
    store = Store(tmp_path / 'hub.sqlite3')
    store.init()
    now = time.time()
    stuck = store.enqueue('x', '2118825757351026886', 'x_official_media_ingest_v1', 'tweet', {},
                          'machine-tweet-v4-api-relations:source-4')
    fresh = store.enqueue('x', '2118825757351026887', 'x_official_media_ingest_v1', 'tweet', {},
                          'machine-tweet-v4-api-relations:source-4')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='retry_scheduled',error_code='HEAVY_RESOURCE_BUSY',"
                   "created_at=?,updated_at=? WHERE job_id=?", (now - 900, now - 15, stuck))
        db.execute("UPDATE jobs SET created_at=?,updated_at=? WHERE job_id=?", (now - 60, now - 1, fresh))

    rows = health_module().deferred_ingests(store, now, 600)

    assert [row['source_id'] for row in rows] == ['2118825757351026886']
    assert rows[0]['error_code'] == 'HEAVY_RESOURCE_BUSY'
    assert rows[0]['state'] == 'retry_scheduled'
    assert rows[0]['waiting_seconds'] >= 900


def test_deferred_ingest_ignores_jobs_that_already_reached_ingest(tmp_path):
    store = Store(tmp_path / 'hub.sqlite3')
    store.init()
    now = time.time()
    ident = store.enqueue('x', '2118825757351026886', 'x_official_media_ingest_v1', 'tweet', {},
                          'machine-tweet-v4-api-relations:source-4')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='processing',created_at=? WHERE job_id=?", (now - 900, ident))

    assert health_module().deferred_ingests(store, now, 600) == []


def test_expired_ingest_reports_silent_two_hour_loss(tmp_path):
    store = Store(tmp_path / 'hub.sqlite3')
    store.init()
    now = time.time()
    ident = store.enqueue('x', '2118825757351026886', 'x_official_media_ingest_v1', 'tweet', {},
                          'machine-tweet-v4-api-relations:source-4')
    result = {'ingestion': {'created': False, 'disposition': 'expired_or_cleared',
                            'error_code': 'OWNER_TWO_HOUR_WINDOW_EXPIRED'}}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=?,updated_at=? WHERE job_id=?",
                   (json.dumps(result), now - 30, ident))

    rows = health_module().expired_ingests(store, now - 3600)

    assert [row['source_id'] for row in rows] == ['2118825757351026886']
    assert rows[0]['error_code'] == 'OWNER_TWO_HOUR_WINDOW_EXPIRED'
    assert rows[0]['disposition'] == 'expired_or_cleared'


def test_expired_ingest_ignores_other_failures(tmp_path):
    store = Store(tmp_path / 'hub.sqlite3')
    store.init()
    now = time.time()
    ident = store.enqueue('x', '2118825757351026886', 'x_official_media_ingest_v1', 'tweet', {},
                          'machine-tweet-v4-api-relations:source-4')
    result = {'ingestion': {'created': False, 'disposition': 'rejected_asset',
                            'error_code': 'X_OFFICIAL_MEDIA_DECODE_FAILED'}}
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='completed',result=?,updated_at=? WHERE job_id=?",
                   (json.dumps(result), now - 30, ident))

    assert health_module().expired_ingests(store, now - 3600) == []
