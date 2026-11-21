# SQL Agent — Natural Language to PostgreSQL

Ask questions about a database in plain English and get back the answer, the SQL that produced it, and the rows. Every generated query passes a deterministic safety layer before it reaches the database.

Built with FastAPI, GPT-4o-mini and PostgreSQL, deployed on Railway against the public Chinook dataset (a music store: artists, albums, tracks, customers, invoices). Every response includes the generated SQL, so an answer can always be checked.

## How it works

The LLM's output can be influenced through the user's question, and prompt injection can't be fixed at the prompt layer. So the controls live outside the model, in code and in database permissions it can't talk its way past.

```
User question
     │
     ▼
 LLM generates SQL          ← may have been manipulated here
     │
     ▼
 ┌─────────────────────────────────────────────┐
 │ VALIDATOR (sqlparse — deterministic)        │
 │  1. Exactly ONE statement                   │  ← stops stacked queries:
 │  2. Statement type must be SELECT           │     SELECT ...; DROP TABLE ...;
 │  3. Keyword denylist over parsed tokens     │  ← redundant third layer
 └─────────────────────────────────────────────┘
     │ unsafe → log as 'blocked', refuse, STOP
     ▼ safe
 Execute on agent_ro        ← read-only role: SELECT grants only,
     │                        session-level read-only, 10s statement timeout
     ▼
 Rows → LLM explains → response
     │
     ▼
 Audit log (agent_log)      ← separate role; agent_ro cannot read or write it
```

| Layer                 | Mechanism                                                   | Nature            |
| --------------------- | ----------------------------------------------------------- | ----------------- |
| Input filtering       | Prompt instructs SELECT-only, refuses modification requests | Probabilistic     |
| **Output validation** | sqlparse structural checks on generated SQL                 | **Deterministic** |
| Role grants           | `agent_ro` holds SELECT and nothing else                    | Deterministic     |
| Session read-only     | `conn.set_read_only(True)` at the protocol level            | Deterministic     |
| Statement timeout     | 10s cap turns runaway queries into retryable errors         | Resource bound    |

The validator's first rule is "exactly one statement", not "no DROP": `SELECT * FROM customer WHERE 1=1; DROP TABLE customer;` starts with SELECT but parses as two statements and is rejected. `SELECT * FROM customer WHERE last_name = 'DROP TABLE'` passes, because the dangerous-looking text is a string literal token, not a keyword. The validator checks token types, not raw text.

**Two database roles.** `agent_ro` runs the generated SQL with SELECT grants only and cannot read the audit log. `agent_log` writes the audit trail with a parameterized INSERT and cannot read any Chinook table, so the audited path can never touch its own evidence. Every request is logged to `query_log` with the question, the generated SQL and one of four outcomes (`executed` / `blocked` / `unanswerable` / `error`).

**Schema introspection.** The schema is read at startup from `pg_catalog` and given to the model in a compact format: columns, foreign keys as arrows (which is how it finds join paths), and up to three example values for columns with 30 or fewer distinct values, so it writes `'USA'` rather than `'United States'`.

**Retry.** When a query fails on the database, the error goes back to the model to correct it, up to 2 retries. Validation blocks never retry: a block means the model produced something forbidden, and retrying invites another attempt.

## API

| Method | Endpoint    | Description                                                             |
| ------ | ----------- | ----------------------------------------------------------------------- |
| `POST` | `/ask`      | Question in; answer, SQL, rows and outcome out                          |
| `GET`  | `/queries`  | The audit log, newest first (`?limit=`, capped at 200)                  |
| `POST` | `/validate` | Run SQL through the validator only; returns the verdict, never executes |
| `GET`  | `/health`   | Checks the database connection (503 if unreachable)                     |

`numeric` columns serialize as JSON strings (`"49.62"`) to keep exact decimal values.

## Architecture

```
app/
├── main.py        # FastAPI endpoints, startup schema warm-up
├── agent.py       # generate → validate → execute → retry → explain
├── validator.py   # the safety layer (sqlparse, no DB or LLM dependency)
├── schema.py      # introspection: columns, FK arrows, example values (cached)
├── db.py          # two connections: read-only (untrusted SQL) + log (audit)
├── models.py      # Pydantic request/response models
└── config.py      # env + constants
```

It is a fixed pipeline with retry rather than a tool-calling agent: for one database with a known schema the sequence is always inspect schema → write query → execute, so the model doesn't need to rediscover it on every request.

## Running locally

Requires Docker and an OpenAI API key.

```bash
docker compose up -d   # Postgres with Chinook and the roles, loaded on first boot

cat > .env << 'ENV'
OPENAI_API_KEY=sk-your-key
AGENT_RO_DSN=host=localhost port=5433 dbname=chinook user=agent_ro password=ro_pass
AGENT_LOG_DSN=host=localhost port=5433 dbname=chinook user=agent_log password=log_pass
ENV

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Open `http://localhost:8080/docs`. If you change the SQL init files after the first boot, run `docker compose down -v`, since init scripts only run on an empty volume.

`tests/check_agent.py` is a manual script that runs questions through the full pipeline and needs the database and an OpenAI key. There is no automated test suite yet. `tests/Test_questions.md` records a run of ten question types against the deployed service.

## Known limitations

- **Data-modifying CTEs**: PostgreSQL allows `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`, which sqlparse types as SELECT. Rule 3 catches the DELETE keyword and the role grants catch it regardless, but rule 2 alone would pass it.
- **The validator does not check table access**: a SELECT against `query_log` passes validation and is stopped by the role grants. Table access is enforced at the database layer on purpose.
- **No result-size bound**: a SELECT returning millions of rows passes every rule; only the 10s statement timeout limits it.
- **Denylist shape on rule 3**: rules 1 and 2 are allowlist-shaped; the keyword list is a redundant layer, not the primary control.
- **Example-value threshold is a heuristic** (30 or fewer distinct values): a 31-value column that matters is skipped.
- **Ambiguous questions are refused rather than clarified**: "show me the top performers" returns `unanswerable`.
- **Schema cached at startup**: assumes schema changes come with a redeploy.
- **Railway connection uses the public endpoint**: private-network DNS (`postgres.railway.internal`) did not resolve reliably at deploy time.
- **Credentials in env vars and init scripts**: acceptable for the public Chinook data; with confidential data, use IAM auth or a secrets manager.
- **No authentication or rate limiting on the API**: both are needed before this is pointed at a confidential database.
- **Nullable `track.album_id`**: inner joins from track to album to artist drop tracks with no album, so an answer can look right and be subtly wrong. This is one reason the SQL is returned with every answer.
