BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS api_usage_details(
 call_id TEXT PRIMARY KEY REFERENCES api_calls(call_id),
 model TEXT, input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL
);
INSERT OR IGNORE INTO schema_migrations VALUES(4,datetime('now'));
COMMIT;
