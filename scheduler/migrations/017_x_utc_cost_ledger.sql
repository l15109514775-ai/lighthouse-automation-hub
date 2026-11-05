BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS x_cost_observations(
 call_id TEXT PRIMARY KEY REFERENCES api_calls(call_id),
 version TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','verified','cross_utc_upper_bound')),
 raw_sha256 TEXT NOT NULL, canonical_sha256 TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL NOT NULL, utc_day TEXT NOT NULL,
 resources TEXT NOT NULL, recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS x_cost_events(
 event_id TEXT PRIMARY KEY, call_id TEXT REFERENCES api_calls(call_id),
 version TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL, at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS x_cost_events_by_call ON x_cost_events(call_id,version,kind);
CREATE TABLE IF NOT EXISTS x_cost_calibrations(
 calibration_id TEXT PRIMARY KEY, version TEXT NOT NULL, record TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS x_cost_control(
 id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL,
 calibration_id TEXT NOT NULL REFERENCES x_cost_calibrations(calibration_id), source_files TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS x_cost_unique_resources AS
 SELECT o.version,o.utc_day,json_extract(j.value,'$.kind') resource_kind,
 json_extract(j.value,'$.id') resource_id,MIN(o.started_at) first_call_started_at,
 COUNT(DISTINCT o.call_id) returned_in_calls,MAX(json_extract(j.value,'$.unit_microusd')) unit_microusd
 FROM x_cost_observations o,json_each(o.resources) j WHERE o.state='verified'
 GROUP BY o.version,o.utc_day,resource_kind,resource_id;
INSERT OR IGNORE INTO schema_migrations VALUES(17,datetime('now'));
COMMIT;
