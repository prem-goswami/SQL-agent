CREATE TABLE IF NOT EXISTS api_keys (
    id         SERIAL PRIMARY KEY,
    key_hash   TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    key_id     INT NOT NULL REFERENCES api_keys(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS session_turns (
    id         SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    question   TEXT NOT NULL,
    sql        TEXT,
    outcome    TEXT NOT NULL,
    answer     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
GRANT SELECT, INSERT ON api_keys, sessions, session_turns TO agent_log;
GRANT USAGE, SELECT ON SEQUENCE api_keys_id_seq, session_turns_id_seq TO agent_log;
