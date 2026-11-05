BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS youtube_videos(
 video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, title TEXT NOT NULL,
 published_at TEXT NOT NULL, url TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('unknown','upcoming','live','ended','ordinary','unavailable')),
 scheduled_start TEXT, actual_start TEXT, actual_end TEXT, duration TEXT,
 first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
 last_api_check REAL, source_api TEXT NOT NULL, metadata TEXT NOT NULL,
 replay_job_id TEXT REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS youtube_open ON youtube_videos(channel_id,state);
CREATE TABLE IF NOT EXISTS source_polls(
 source_key TEXT PRIMARY KEY, checked_at REAL NOT NULL,
 last_success_at REAL, error_code TEXT, metadata TEXT NOT NULL DEFAULT '{}',
 next_check_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO schema_migrations VALUES(5,datetime('now'));
COMMIT;
