import sqlite3

from scripts.verify_recovery_database import inspect_file


def test_restored_copy_detects_rowid_storage_class_and_schema_changes(tmp_path):
    original, restored = tmp_path / "before.db", tmp_path / "restored.db"
    with sqlite3.connect(original) as db:
        db.executescript("""
            CREATE TABLE receipt(value);
            INSERT INTO receipt(rowid,value) VALUES(2,1),(5,'1');
            CREATE TABLE task(id TEXT PRIMARY KEY, payload BLOB) WITHOUT ROWID;
            INSERT INTO task VALUES('01',x'31');
            CREATE VIEW current_tasks AS SELECT * FROM task;
        """)
        with sqlite3.connect(restored) as dest:
            db.backup(dest)
    expected = inspect_file(original)
    assert expected == inspect_file(restored)
    with sqlite3.connect(restored) as db:
        db.execute("UPDATE receipt SET rowid=3 WHERE rowid=2")
    assert expected["tables"]["receipt"] != inspect_file(restored)["tables"]["receipt"]
    with sqlite3.connect(restored) as db:
        db.execute("UPDATE receipt SET rowid=2 WHERE rowid=3")
        db.execute("UPDATE task SET payload='1'")
        db.execute("DROP VIEW current_tasks")
    actual = inspect_file(restored)
    assert expected["tables"]["receipt"] == actual["tables"]["receipt"]
    assert expected["tables"]["task"] != actual["tables"]["task"]
    assert expected["schema"] != actual["schema"]
