BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS extended_web_source_bindings(
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), discovery_job_id TEXT NOT NULL REFERENCES jobs(job_id),
 payload_sha256 TEXT NOT NULL, packet_file TEXT NOT NULL, packet_sha256 TEXT NOT NULL,
 captured_at REAL NOT NULL, bound_at REAL NOT NULL
);
INSERT OR IGNORE INTO schema_migrations VALUES(14,datetime('now'));
COMMIT;
