BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS model_request_profiles(
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 phase TEXT NOT NULL CHECK(phase IN ('translation','quality')),
 request_key TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 contract_version TEXT NOT NULL,
 protocol TEXT NOT NULL CHECK(protocol IN ('chat_completions','openai_responses')),
 endpoint TEXT NOT NULL, requested_model TEXT NOT NULL,
 max_output_tokens INTEGER NOT NULL CHECK(max_output_tokens BETWEEN 128 AND 8192),
 legacy_key TEXT NOT NULL, bound_at REAL NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN ('first_invocation','existing_receipt')),
 PRIMARY KEY(job_id,phase)
);
INSERT OR IGNORE INTO schema_migrations VALUES(15,datetime('now'));
COMMIT;
