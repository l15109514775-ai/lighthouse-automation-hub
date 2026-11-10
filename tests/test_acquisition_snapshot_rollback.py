import sqlite3

import pytest

from scripts.test_acquisition_snapshot_rollback import fingerprint, idle_clone


def test_compare_every_field_and_duplicate_row_but_allow_new_heartbeat(tmp_path):
    path = tmp_path / 'fixture.db'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE jobs(state TEXT, attempts INTEGER, receipt BLOB);
            CREATE TABLE heartbeats(name TEXT, at REAL);
            INSERT INTO jobs VALUES ('publish_unknown', 2, X'12'), ('publish_unknown', 2, X'12');
        """)
    before = fingerprint(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO heartbeats VALUES('producer', 10)")
    assert fingerprint(path) == before
    with sqlite3.connect(path) as db:
        db.execute('UPDATE jobs SET attempts=3 WHERE rowid=1')
    assert fingerprint(path)[1]['jobs'] != before[1]['jobs']
    with sqlite3.connect(path) as db:
        db.execute('UPDATE jobs SET attempts=2 WHERE rowid=1')
        db.execute('DELETE FROM jobs WHERE rowid=2')
    assert fingerprint(path)[1]['jobs'][0] == 1


def test_snapshot_gate_allows_historical_review_and_refuses_live_download(tmp_path):
    path = tmp_path / 'fixture.db'
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE jobs(job_type TEXT, state TEXT);
            CREATE TABLE replay_fifo_v4(state TEXT);
            INSERT INTO replay_fifo_v4 VALUES ('hold');
            INSERT INTO jobs VALUES ('youtube_replay_download_v4', 'awaiting_review');
        """)
    idle_clone(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET state='processing'")
    with pytest.raises(ValueError, match='SNAPSHOT_HAS_ACTIVE_ACQUISITION_WORK'):
        idle_clone(path)
