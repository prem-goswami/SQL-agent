-- Read-only role: executes LLM-generated SQL. SELECT and nothing else.
CREATE ROLE agent_ro WITH LOGIN PASSWORD 'ro_pass';
GRANT CONNECT ON DATABASE chinook TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;

-- Write role: writes the audit log. Owns and writes only that one table.
CREATE ROLE agent_log WITH LOGIN PASSWORD 'log_pass';
GRANT CONNECT ON DATABASE chinook TO agent_log;
GRANT USAGE ON SCHEMA public TO agent_log;

CREATE TABLE query_log (
    id           SERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    question     TEXT NOT NULL,
    generated_sql TEXT,
    outcome      TEXT NOT NULL,      -- 'executed' | 'blocked' | 'error'
    block_reason TEXT,
    error_message TEXT
);

GRANT INSERT, SELECT ON query_log TO agent_log;
GRANT USAGE, SELECT ON SEQUENCE query_log_id_seq TO agent_log;

-- The read-only role must NOT see the audit log — it logs itself, it doesn't read itself.
REVOKE ALL ON query_log FROM agent_ro;