-- Additive only: one shared admission/pacing record in the existing task DB.
CREATE TABLE IF NOT EXISTS youtube_acquisition_access (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
    error_code TEXT,
    not_before REAL NOT NULL DEFAULT 0,
    next_request_at REAL NOT NULL DEFAULT 0,
    next_metadata_at REAL NOT NULL DEFAULT 0,
    next_job_at REAL NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO youtube_acquisition_access(singleton) VALUES(1);
