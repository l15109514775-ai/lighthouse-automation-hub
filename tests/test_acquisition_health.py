import json
from services.hub.acquisition_health import healthy, NAMES


def test_health_requires_both_current_boot_threads(runtime, tmp_path):
    settings, store = runtime
    marker = tmp_path / 'epoch.json'
    marker.write_text(json.dumps({'started_at': 100}), 'utf-8')
    assert not healthy(settings.db_path, marker, now=110)
    with store.transaction() as db:
        db.execute('INSERT INTO heartbeats VALUES(?,?)', (NAMES[0], 109))
        db.execute('INSERT INTO heartbeats VALUES(?,?)', (NAMES[1], 99))
    assert not healthy(settings.db_path, marker, now=110)
    with store.transaction() as db:
        db.execute('UPDATE heartbeats SET at=108 WHERE name=?', (NAMES[1],))
    assert healthy(settings.db_path, marker, now=110)
    # Active consumer cannot hide a stopped producer.
    with store.transaction() as db:
        db.execute('UPDATE heartbeats SET at=300 WHERE name=?', (NAMES[0],))
    assert not healthy(settings.db_path, marker, now=301)


def test_health_does_not_create_missing_database(tmp_path):
    db = tmp_path / 'missing.sqlite3'
    marker = tmp_path / 'epoch.json'
    marker.write_text('{"started_at":100}', 'utf-8')
    assert not healthy(db, marker, now=110)
    assert not db.exists()
