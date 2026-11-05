BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS bilibili_accounts(
 open_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, name TEXT NOT NULL,
 scopes TEXT NOT NULL, verified_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bilibili_uploads(
 publication_key TEXT PRIMARY KEY,
 account_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, file_bytes INTEGER NOT NULL,
 metadata TEXT NOT NULL, state TEXT NOT NULL,
 encrypted_upload_token TEXT, next_part INTEGER NOT NULL DEFAULT 1,
 part_bytes INTEGER NOT NULL, resource_id TEXT, last_error_code TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bilibili_parts(
 publication_key TEXT NOT NULL REFERENCES bilibili_uploads(publication_key),
 part_number INTEGER NOT NULL, content_sha256 TEXT NOT NULL, byte_count INTEGER NOT NULL,
 completed_at REAL NOT NULL, PRIMARY KEY(publication_key,part_number)
);
INSERT OR IGNORE INTO schema_migrations VALUES(6,datetime('now'));
COMMIT;
