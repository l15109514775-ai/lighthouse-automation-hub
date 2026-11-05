BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS bilibili_credentials(
 account_id TEXT PRIMARY KEY, app_id TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>=1),
 encrypted_tokens TEXT NOT NULL, access_expires_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('validating','active','refreshing','refresh_unknown','auth_expired')),
 updated_at REAL NOT NULL, error_code TEXT,
 validation_attempts INTEGER NOT NULL DEFAULT 0, next_check_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bilibili_token_refreshes(
 refresh_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES bilibili_credentials(account_id),
 generation INTEGER NOT NULL, refresh_fingerprint TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('started','confirmed','unknown','rejected')),
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error_code TEXT,
 UNIQUE(account_id,generation), UNIQUE(account_id,refresh_fingerprint)
);
INSERT OR IGNORE INTO schema_migrations VALUES(8,datetime('now'));
COMMIT;
