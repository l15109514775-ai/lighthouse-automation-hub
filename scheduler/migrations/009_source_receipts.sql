BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS source_http_receipts(
 call_id TEXT PRIMARY KEY REFERENCES api_calls(call_id),
 discovery_job_id TEXT NOT NULL REFERENCES jobs(job_id),
 method TEXT NOT NULL CHECK(method='GET'), host TEXT NOT NULL CHECK(host='api.x.com'),
 path TEXT NOT NULL, params TEXT NOT NULL,
 raw_sha256 TEXT NOT NULL, canonical_sha256 TEXT NOT NULL,
 transport_mode TEXT NOT NULL CHECK(transport_mode IN ('default_tls','custom_transport')),
 captured_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tweet_source_bindings(
 binding_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 discovery_job_id TEXT NOT NULL REFERENCES jobs(job_id),
 user_call_id TEXT NOT NULL REFERENCES source_http_receipts(call_id),
 timeline_call_id TEXT NOT NULL REFERENCES source_http_receipts(call_id),
 payload_sha256 TEXT NOT NULL, bound_at REAL NOT NULL,
 UNIQUE(job_id,user_call_id,timeline_call_id)
);
CREATE INDEX IF NOT EXISTS tweet_receipts_by_job ON tweet_source_bindings(job_id,bound_at);
INSERT OR IGNORE INTO schema_migrations VALUES(9,datetime('now'));
COMMIT;
