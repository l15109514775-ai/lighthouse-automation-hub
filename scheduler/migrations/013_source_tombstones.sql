BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS source_tombstones(
 source_platform TEXT NOT NULL,
 source_id TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason='OWNER_BACKLOG_CLEARED'),
 marked_at REAL NOT NULL,
 evidence_job_id TEXT NOT NULL REFERENCES jobs(job_id),
 origin TEXT NOT NULL CHECK(origin IN ('owner_clear_all','policy_window_expired','legacy_job_error_observed')),
 PRIMARY KEY(source_platform,source_id)
);
-- Preserve the evidence we actually have: this is the legacy job's recorded
-- update time, not a newly invented owner approval or reconstructed first clear.
INSERT OR IGNORE INTO source_tombstones(source_platform,source_id,reason,marked_at,evidence_job_id,origin)
 SELECT source_platform,source_id,'OWNER_BACKLOG_CLEARED',updated_at,job_id,'legacy_job_error_observed'
 FROM jobs WHERE source_platform='x' AND job_type='tweet_process' AND error_code='OWNER_BACKLOG_CLEARED'
 ORDER BY updated_at,job_id;
INSERT OR IGNORE INTO schema_migrations VALUES(13,datetime('now'));
COMMIT;
