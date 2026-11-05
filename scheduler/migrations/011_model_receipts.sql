BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS model_call_receipts(
 request_key TEXT PRIMARY KEY REFERENCES model_requests(request_key),
 call_id TEXT NOT NULL UNIQUE REFERENCES api_calls(call_id), job_id TEXT NOT NULL REFERENCES jobs(job_id),
 phase TEXT NOT NULL CHECK(phase IN ('translation','quality')),
 contract_version TEXT NOT NULL, protocol TEXT NOT NULL, endpoint TEXT NOT NULL,
 requested_model TEXT NOT NULL, returned_model TEXT,
 max_output_tokens INTEGER NOT NULL,
 source_sha256 TEXT NOT NULL, translation_sha256 TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, request_wire_sha256 TEXT NOT NULL,
 response_wire_sha256 TEXT NOT NULL, response_sha256 TEXT NOT NULL,
 transport_mode TEXT NOT NULL CHECK(transport_mode IN ('default_tls','custom_transport')),
 finished_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS model_receipts_by_job ON model_call_receipts(job_id,phase,finished_at);
INSERT OR IGNORE INTO schema_migrations VALUES(11,datetime('now'));
COMMIT;
