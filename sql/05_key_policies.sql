-- Access policies attached to API keys, and key revocation.
-- A key with no policy sees every Chinook table.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS policy JSONB;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

GRANT UPDATE (revoked_at) ON api_keys TO agent_log;
