BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS api_resource_usage(
 call_id TEXT PRIMARY KEY REFERENCES api_calls(call_id),
 pricing_version TEXT NOT NULL, pricing_source TEXT NOT NULL,
 endpoint_kind TEXT NOT NULL CHECK(endpoint_kind IN ('user_lookup','timeline','post_detail')),
 max_posts INTEGER NOT NULL CHECK(max_posts BETWEEN 0 AND 100),
 max_users INTEGER NOT NULL CHECK(max_users BETWEEN 0 AND 100),
 returned_posts INTEGER CHECK(returned_posts>=0), returned_users INTEGER CHECK(returned_users>=0),
 post_unit_usd REAL NOT NULL CHECK(post_unit_usd>=0), user_unit_usd REAL NOT NULL CHECK(user_unit_usd>=0),
 reserved_upper_bound_usd REAL NOT NULL CHECK(reserved_upper_bound_usd>0),
 settled_upper_bound_usd REAL CHECK(settled_upper_bound_usd>=0),
 state TEXT NOT NULL CHECK(state IN ('reserved','estimated','error_upper_bound','unknown_upper_bound')),
 created_at REAL NOT NULL, settled_at REAL
);
INSERT OR IGNORE INTO schema_migrations VALUES(16,datetime('now'));
COMMIT;
