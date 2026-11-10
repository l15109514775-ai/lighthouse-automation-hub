"""Compare two SQLite snapshots, including schema and every stored row.

Run on private recovery copies. Reports contain hashes and counts only.
"""
import argparse
import hashlib
import json
import sqlite3
import struct
from pathlib import Path


def row_digest(row):
    h = hashlib.sha256()
    for value in row:
        if value is None:
            tag, data = b"n", b""
        elif isinstance(value, int):
            tag, data = b"i", struct.pack(">q", value)
        elif isinstance(value, float):
            tag, data = b"r", struct.pack(">d", value)
        elif isinstance(value, bytes):
            tag, data = b"b", value
        elif isinstance(value, str):
            tag, data = b"t", value.encode("utf-8")
        else:
            raise TypeError("unsupported SQLite storage class")
        h.update(tag + len(data).to_bytes(8, "big"))
        h.update(data)
    return h.digest()


def rows_summary(rows):
    # Sorting fixed-size hashes preserves duplicates without relying on text
    # collations, insertion order, or a particular SQLite query plan.
    hashes = sorted(row_digest(row) for row in rows)
    h = hashlib.sha256()
    for digest in hashes:
        h.update(digest)
    return {"rows": len(hashes), "sha256": h.hexdigest()}


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def fingerprint(db):
    """Caller holds a single read transaction on this database."""
    if not db.in_transaction:
        raise ValueError("an explicit snapshot transaction is required")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign key check failed")
    result = {
        "schema": rows_summary(db.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_schema")),
        "application_id": db.execute("PRAGMA application_id").fetchone()[0],
        "user_version": db.execute("PRAGMA user_version").fetchone()[0],
        "tables": {},
    }
    if sqlite3.sqlite_version_info < (3, 37):
        raise RuntimeError("SQLite 3.37 or newer is required")
    for schema, name, kind, _, without_rowid, _ in db.execute("PRAGMA main.table_list"):
        if schema != "main" or name == "sqlite_schema" or kind == "view":
            continue
        if kind != "table":
            raise ValueError("unsupported virtual or shadow table: " + name)
        table = quote_identifier(name)
        columns = {row[1].lower() for row in db.execute(f"PRAGMA main.table_xinfo({table})")}
        fields = "*"
        if not without_rowid:
            alias = next((key for key in ("rowid", "_rowid_", "oid") if key not in columns), None)
            if alias is None:
                raise ValueError("rowid is inaccessible for table: " + name)
            fields = f"{alias}, *"
        result["tables"][name] = rows_summary(db.execute(f"SELECT {fields} FROM main.{table}"))
    return result


def inspect_file(path):
    with sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        return fingerprint(db)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("restored", type=Path)
    args = parser.parse_args()
    left, right = inspect_file(args.reference), inspect_file(args.restored)
    changed = sorted(k for k in left["tables"].keys() | right["tables"].keys()
                     if left["tables"].get(k) != right["tables"].get(k))
    print(json.dumps({"matched": left == right, "tables": len(left["tables"]), "changed_tables": changed,
                      "schema_matched": left["schema"] == right["schema"]}))
    raise SystemExit(int(left != right))
