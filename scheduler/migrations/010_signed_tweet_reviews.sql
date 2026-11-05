BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS tweet_review_requests(
 request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
 manifest TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('pending','accepted','rejected','revoked','expired')),
 reviewer_id TEXT, attestation TEXT, signature TEXT, decided_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_tweet_review
 ON tweet_review_requests(job_id) WHERE decision='pending';
CREATE TABLE IF NOT EXISTS tweet_review_bindings(
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
 request_id TEXT NOT NULL UNIQUE REFERENCES tweet_review_requests(request_id), manifest_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tweet_review_decisions(
 decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES tweet_review_requests(request_id),
 reviewer_id TEXT NOT NULL, decision TEXT NOT NULL, attestation TEXT NOT NULL,
 signature TEXT NOT NULL, decided_at REAL NOT NULL
);
INSERT OR IGNORE INTO schema_migrations VALUES(10,datetime('now'));
COMMIT;
