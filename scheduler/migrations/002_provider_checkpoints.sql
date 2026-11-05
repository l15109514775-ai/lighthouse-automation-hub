BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS model_requests(request_key TEXT PRIMARY KEY,
 state TEXT NOT NULL,response TEXT,error_code TEXT);
CREATE TABLE IF NOT EXISTS source_cursors(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT OR IGNORE INTO schema_migrations VALUES(2,datetime('now'));
COMMIT;
