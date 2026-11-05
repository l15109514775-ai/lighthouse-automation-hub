-- Explicitly initialized by ReplyEvidence.initialize(); not a live migration.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS reply_machine_receipts(
 receipt_id TEXT PRIMARY KEY, discovery_job_id TEXT NOT NULL REFERENCES jobs(job_id),
 capture_id TEXT NOT NULL UNIQUE, parent_post_id TEXT NOT NULL, reply_post_id TEXT NOT NULL,
 packet_sha256 TEXT NOT NULL, receipt_file TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
 recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_machine_translation_checks(
 check_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL REFERENCES reply_machine_receipts(receipt_id),
 parent_job_id TEXT NOT NULL REFERENCES jobs(job_id), reply_job_id TEXT NOT NULL REFERENCES jobs(job_id),
 result_file TEXT NOT NULL, result_sha256 TEXT NOT NULL, checked_at REAL NOT NULL
);
COMMIT;
