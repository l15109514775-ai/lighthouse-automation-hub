BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS publication_reviews(
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
 reviewer TEXT NOT NULL, reviewed_at REAL NOT NULL,
 source_sha256 TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
 rights_evidence TEXT NOT NULL, account_id TEXT NOT NULL,
 review_kind TEXT NOT NULL CHECK(review_kind IN ('human','synthetic_test'))
);
INSERT OR IGNORE INTO schema_migrations VALUES(3,datetime('now'));
COMMIT;
