BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 job_id TEXT PRIMARY KEY,
 source_platform TEXT NOT NULL,
 source_id TEXT NOT NULL,
 job_type TEXT NOT NULL,
 queue TEXT NOT NULL CHECK(queue IN ('tweet','video','system')),
 state TEXT NOT NULL CHECK(state IN ('pending','processing','awaiting_review','retry_scheduled','publish_unknown','awaiting_moderation','auth_expired','upload_pending','completed','failed')),
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
 external_publish_id TEXT, policy_version TEXT NOT NULL,
 payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
 stage TEXT NOT NULL DEFAULT 'discovered', next_run REAL NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until REAL,
 UNIQUE(source_platform,source_id,job_type,policy_version)
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(queue,state,next_run);
CREATE TABLE IF NOT EXISTS pipelines(name TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO pipelines(name) VALUES('tweet'),('video'),('system');
CREATE TABLE IF NOT EXISTS publications(
 idempotency_key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
 platform TEXT NOT NULL, source_platform TEXT NOT NULL, source_id TEXT NOT NULL,
 account_id TEXT NOT NULL, kind TEXT NOT NULL,
 state TEXT NOT NULL, external_id TEXT, external_url TEXT, updated_at REAL NOT NULL,
 UNIQUE(platform,source_platform,source_id,account_id,kind)
);
CREATE TABLE IF NOT EXISTS api_calls(
 call_id TEXT PRIMARY KEY, provider TEXT NOT NULL, job_id TEXT,
 started_at REAL NOT NULL, finished_at REAL, cost_usd REAL NOT NULL,
 state TEXT NOT NULL, error_code TEXT, units INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS api_usage ON api_calls(provider,started_at);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, occurred_at REAL NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_dedup(key TEXT PRIMARY KEY, last_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, principal TEXT NOT NULL,
 action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules(name TEXT PRIMARY KEY, next_due REAL NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeats(name TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
 callback TEXT NOT NULL, encrypted_secret TEXT NOT NULL,
 expires_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 0,
 cursor INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries(
 subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
 event_seq INTEGER NOT NULL REFERENCES events(seq), state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0,
 error_code TEXT, PRIMARY KEY(subscription_id,event_seq)
);
INSERT OR IGNORE INTO schema_migrations VALUES(1,datetime('now'));
COMMIT;
