# SQL Agent — Natural Language to PostgreSQL

Ask questions about a database in plain English and get back the answer, the SQL that produced it, and the rows. Every generated query passes a deterministic safety layer before it reaches the database, and every request is tied to an API key with its own role, rate limit and access policy.

Built with FastAPI, GPT-4o-mini and PostgreSQL, running against the public Chinook dataset (a music store: artists, albums, tracks, customers, invoices). Every response includes the generated SQL, so an answer can always be checked.

## How it works

The LLM's output can be influenced through the user's question, and prompt injection can't be fixed at the prompt layer. So the controls live outside the model, in code and in database permissions it can't talk its way past.

```
Request with X-API-Key
     │
     ▼
 Key lookup → role, policy, per-key rate limit      (401 / 403 / 429)
     │
     ▼
 LLM generates SQL          ← sees only the tables the key's policy lists;
     │                        may reply CLARIFY: <question> or CANNOT_ANSWER
     ▼
 ┌─────────────────────────────────────────────┐
 │ VALIDATOR (sqlparse — deterministic)        │
 │  one statement, SELECT only, no INTO,       │
 │  no row locks, no data-modifying keywords,  │
 │  no side-effecting or server functions      │
 └─────────────────────────────────────────────┘
     │ unsafe → log as 'blocked' with the rule, refuse, STOP
     ▼ safe
 Execute on agent_ro        ← SELECT grants only, read-only transactions,
     │                        10s statement timeout, row cap with paging
     ▼
 Rows → LLM explains → response
     │
     ▼
 Audit log (agent_log)      ← separate role; agent_ro cannot read or write it
```

| Layer                 | Mechanism                                                         | Nature            |
| --------------------- | ----------------------------------------------------------------- | ----------------- |
| Authentication        | `X-API-Key`, hashed at rest, admin / user roles, revocable        | Deterministic     |
| Rate limiting         | Sliding window per key (30 requests / 60 s by default)            | Deterministic     |
| Input filtering       | Prompt instructs SELECT-only, refuses modification requests       | Probabilistic     |
| **Output validation** | sqlparse structural checks on generated SQL, each with a rule name | **Deterministic** |
| Role grants           | `agent_ro` holds SELECT and nothing else                          | Deterministic     |
| Session read-only     | `SET default_transaction_read_only = on` on every connection      | Deterministic     |
| Statement timeout     | 10s cap turns runaway queries into retryable errors               | Resource bound    |
| Row cap               | 100 rows per response, the rest behind a cursor                   | Resource bound    |

### The validator

Each rejection names the rule that caught it. `/validate` and blocked `/ask` responses return it, and `query_log.block_rule` stores it.

| Rule               | Rejects                                                                        |
| ------------------ | ------------------------------------------------------------------------------ |
| `empty`            | No SQL, or nothing left after removing code fences                             |
| `size`             | Over 10,000 characters                                                         |
| `parse`            | Input the parser cannot analyse; the gate never raises                         |
| `single_statement` | `SELECT 1; DROP TABLE customer`                                                |
| `statement_type`   | Anything that is not a SELECT                                                  |
| `select_into`      | `SELECT ... INTO`, which creates a table                                       |
| `row_locking`      | `FOR UPDATE`, `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR KEY SHARE`                |
| `data_modifying`   | `DELETE`, `UPDATE`, `INSERT`, DDL and grants, including inside a `WITH`        |
| `function`         | `pg_sleep`, `set_config`, `nextval`, file and large-object access, `dblink`, … |

It checks token types, not raw text: `SELECT * FROM customer WHERE last_name = 'DROP TABLE'` passes, because the dangerous-looking text is a string literal, and `SELECT comment FROM review` passes, because `comment` is a column name there.

**Two database roles.** `agent_ro` runs the generated SQL with SELECT grants only and cannot read the audit log or the key and session tables. `agent_log` writes the audit trail, keys and sessions with parameterized statements and cannot read any Chinook table.

**Schema introspection.** The schema is read at startup from `pg_catalog` and given to the model in a compact format: columns, foreign keys as arrows (which is how it finds join paths), and up to three example values for columns with 30 or fewer distinct values, so it writes `'USA'` rather than `'United States'`.

**Retry.** When a query fails on the database, the error goes back to the model to correct it, up to 2 retries. Validation blocks never retry: a block means the model produced something forbidden, and retrying invites another attempt.

## API keys and access policies

Every endpoint except `/health` needs an `X-API-Key` header.

```bash
python -m app.keys create --role admin
python -m app.keys create --role user --policy policies/analyst_brazil_canada.json
python -m app.keys list
python -m app.keys revoke 3
```

`create` prints the key once; only its SHA-256 hash is stored. Admin keys can read `/queries`; user keys cannot. `GET /keys/me` shows the calling key's role and policy.

A policy is a JSON file stored with the key when it is created. It is checked against the live schema, so a misspelt table or column is rejected at creation time.

```json
{
  "name": "analyst_brazil_canada",
  "tables": ["customer", "invoice", "invoice_line", "track", "album", "artist", "genre", "media_type"],
  "columns": { "customer": { "deny": ["email", "phone", "fax", "address", "postal_code"] } },
  "rows": {
    "customer": { "country": ["Brazil", "Canada"] },
    "invoice":  { "billing_country": ["Brazil", "Canada"] }
  }
}
```

| Section   | Meaning                                                                      |
| --------- | ---------------------------------------------------------------------------- |
| `tables`  | The tables the key may use. Omitted means every Chinook table.               |
| `columns` | Per table, columns the key may not read.                                     |
| `rows`    | Per table, column → allowed values; a row is visible when all of them match. |

**Current status:** `tables` limits the schema description the model is given, so a scoped key's model never hears about the other tables. That is all policies do today. `columns` and `rows` are validated and stored but not applied anywhere, and nothing stops a query the model writes from reading a table, column or row outside the policy. Two examples are in `policies/`.

## Conversations, clarification and paging

`POST /sessions` returns a `session_id`. Pass it to `/ask` and follow-up questions ("and the next one?") are answered with the last five questions and their SQL in context. `GET /sessions/{session_id}` lists the turns. Sessions belong to the key that created them and expire after 24 hours.

When a question can reasonably mean more than one thing, the model asks back instead of guessing: `/ask` returns `outcome: needs_clarification` with the question in `answer`, and nothing runs.

Results are capped at 100 rows. A capped response sets `truncated: true` and a `next_cursor`; `GET /rows?cursor=...` returns the next page in the same shape. Cursors belong to the key that received them and expire after 15 minutes.

## API

| Method | Endpoint                  | Description                                                             |
| ------ | ------------------------- | ----------------------------------------------------------------------- |
| `POST` | `/ask`                    | Question (and optional `session_id`) in; answer, SQL, rows, outcome out |
| `GET`  | `/rows?cursor=`           | The next page of a capped result                                        |
| `POST` | `/sessions`               | Start a conversation                                                    |
| `GET`  | `/sessions/{session_id}`  | A conversation's turns                                                  |
| `POST` | `/validate`               | Run SQL through the validator only; returns the verdict and rule        |
| `GET`  | `/queries`                | The audit log, newest first (`?limit=`, capped at 200); admin only      |
| `GET`  | `/keys/me`                | The calling key's id, role and policy                                   |
| `GET`  | `/health`                 | Database and audit-log status (no key needed)                           |

Outcomes: `executed`, `blocked`, `unanswerable`, `needs_clarification`, `error`. `numeric` columns serialize as JSON strings (`"49.62"`) to keep exact decimal values.

## Architecture

```
app/
├── main.py        # FastAPI endpoints, startup schema warm-up and audit-log check
├── agent.py       # generate → validate → execute → retry → explain; paging cursors
├── validator.py   # the safety layer (sqlparse, no DB or LLM dependency)
├── schema.py      # introspection: columns, FK arrows, example values (cached)
├── auth.py        # X-API-Key lookup, roles, per-key rate limit
├── keys.py        # key CLI: create / list / revoke
├── policies.py    # policy format and validation
├── sessions.py    # conversation storage
├── eval.py        # accuracy harness over eval/gold_cases.json
├── db.py          # two connections: read-only (untrusted SQL) + log (audit, keys, sessions)
├── models.py      # Pydantic request/response models
└── config.py      # env + constants
sql/               # 01 data, 02 roles and audit log, 03+ migrations, applied in order
policies/          # example policies
eval/              # gold questions with reference SQL
tests/             # offline pytest suite, plus a manual end-to-end script
```

It is a fixed pipeline with retry rather than a tool-calling agent: for one database with a known schema the sequence is always inspect schema → write query → execute, so the model doesn't need to rediscover it on every request.

## Running locally

Requires Docker and an OpenAI API key.

```bash
docker compose up -d   # Postgres with Chinook, roles and every migration, loaded on first boot

cat > .env << 'ENV'
OPENAI_API_KEY=sk-your-key
AGENT_RO_DSN=host=localhost port=5433 dbname=chinook user=agent_ro password=ro_pass
AGENT_LOG_DSN=host=localhost port=5433 dbname=chinook user=agent_log password=log_pass
ENV

pip install -r requirements.txt
python -m app.keys create --role admin
uvicorn app.main:app --reload --port 8080
```

Open `http://localhost:8080/docs`. If you change the SQL init files after the first boot, run `docker compose down -v`, since init scripts only run on an empty volume.

**Upgrading an existing database.** A database created before the migrations needs `sql/03_query_log_block_rule.sql`, `sql/04_keys_sessions.sql` and `sql/05_key_policies.sql`, in that order, run as the database owner. Until 03 has run, `/health` reports `degraded` and names the missing `block_rule` column, and blocked queries are still logged without it.

## Evaluation and tests

```bash
python -m app.eval              # the model against eval/gold_cases.json (needs the DB and OpenAI)
python -m app.eval --use-gold   # scores the gold SQL itself, no model calls
```

A case passes when the candidate's rows match the gold rows as a multiset. The last line printed is JSON: `{"cases": 33, "passed": 33, "accuracy": 1.0}`.

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs offline, with no database, network or OpenAI key: the validator, the agent loop with the model and database replaced, the API with keys, roles, rate limits and sessions, and policy parsing. `tests/check_agent.py` is a manual end-to-end script that needs the database and an OpenAI key; `tests/Test_questions.md` records an earlier run of ten question types.

## Known limitations

- **Policies are only applied to the prompt** (see above): a scoped key is protected from the model choosing other tables, not from the model being steered into them.
- **Rate limits, paging cursors and the schema cache live in process memory**: with several workers each keeps its own, and a restart clears them.
- **Example-value threshold is a heuristic** (30 or fewer distinct values): a 31-value column that matters is skipped.
- **Schema cached at startup**: assumes schema changes come with a redeploy.
- **Railway connection uses the public endpoint**: private-network DNS (`postgres.railway.internal`) did not resolve reliably at deploy time.
- **Credentials in env vars and init scripts**: acceptable for the public Chinook data; with confidential data, use IAM auth or a secrets manager.
- **Nullable `track.album_id`**: inner joins from track to album to artist drop tracks with no album, so an answer can look right and be subtly wrong. This is one reason the SQL is returned with every answer.
