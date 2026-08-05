# SQL Agent — Natural Language to PostgreSQL

Ask questions about a database in plain English. Get back the answer, the SQL that produced it, and the rows — every query validated by a deterministic safety layer before it touches the database.

Built with FastAPI, GPT-4o-mini, and PostgreSQL. Deployed on Railway against the Chinook sample database (a music store: artists, albums, tracks, customers, invoices).

**Live demo:** `https://sql-agent-production-a225.up.railway.app/docs`

```bash
curl -X POST https://YOUR-APP.up.railway.app/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which artist has the most tracks?"}'
```

```json
{
  "answer": "The artist with the most tracks is Iron Maiden, with 213 tracks.",
  "sql": "SELECT artist.name, COUNT(track.track_id) AS track_count\nFROM artist\nJOIN album ON artist.artist_id = album.artist_id\nJOIN track ON album.album_id = track.album_id\nGROUP BY artist.name\nORDER BY track_count DESC\nLIMIT 1;",
  "outcome": "executed",
  "attempts": 1
}
```

The model was never told how `artist`, `album`, and `track` connect — it traced the two-hop join path from an introspected schema description. Every response includes the generated SQL, because **a SQL agent that returns only prose is unverifiable**.

---

## Why this is more than a text-to-SQL demo

Most text-to-SQL examples hand an LLM a database connection and hope. This one is built around a single uncomfortable fact: **the LLM's output is attacker-influenceable through the user's question**, and prompt injection cannot be fixed at the prompt layer — because the prompt layer is exactly what's compromised.

So the defense lives outside the model, in deterministic code the model cannot talk its way past:

### The safety layer

```
User question
     │
     ▼
 LLM generates SQL          ← may have been manipulated here
     │
     ▼
 ┌─────────────────────────────────────────────┐
 │ VALIDATOR (sqlparse — deterministic)        │
 │  1. Exactly ONE statement                   │  ← kills stacked queries:
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

**Five independent layers**, each with a different failure mode:

| Layer | Mechanism | Nature |
|---|---|---|
| Input filtering | Prompt instructs SELECT-only, refuses modification requests | Probabilistic |
| **Output validation** | sqlparse structural checks on generated SQL | **Deterministic** |
| Role grants | `agent_ro` holds SELECT and nothing else | Deterministic |
| Session read-only | `conn.set_read_only(True)` at the protocol level | Deterministic |
| Statement timeout | 10s cap converts runaway queries into retryable errors | Resource bound |

The validator's first rule is **not** "no DROP" — it's "exactly one statement." `SELECT * FROM customer WHERE 1=1; DROP TABLE customer;` starts with SELECT and passes any prefix check; the parser sees two statements and rejects it before a single keyword is examined. Meanwhile `SELECT * FROM customer WHERE name = 'DROP TABLE'` correctly **passes** — the dangerous-looking text is a string literal token, not a keyword, and the validator checks token *types*, not raw text. Parsing beats blacklisting because it produces more true positives and fewer false positives at the same time.

Try it live — the `/validate` endpoint feeds SQL straight into the validator without executing:

```bash
curl -X POST https://YOUR-APP.up.railway.app/validate \
  -H "Content-Type: application/json" \
  -d '{"sql": "DELETE FROM invoice;"}'
# → {"is_safe": false, "reason": "Statement type is DELETE, not SELECT."}
```

### Two-role audit architecture

The application makes two categorically different kinds of database call, and they run as different PostgreSQL roles:

- **`agent_ro`** — executes LLM-generated SQL. `SELECT` grants only. Cannot write anywhere; cannot even *read* the audit log.
- **`agent_log`** — writes the audit trail via a parameterized INSERT (the generated SQL is a bound value, never concatenated). Cannot read any Chinook table.

The principle: **audit infrastructure must never be writable by the thing being audited.** If a validator bug ever let hostile SQL through, it still couldn't touch the evidence — and the DELETE would die at the database on grants anyway. Verified directly:

```sql
SELECT has_table_privilege('agent_ro', 'track', 'SELECT');     -- t
SELECT has_table_privilege('agent_ro', 'query_log', 'SELECT'); -- f
```

Every request is logged to `query_log` — question, generated SQL verbatim, timestamp, and one of four outcomes (`executed` / `blocked` / `unanswerable` / `error`) — queryable via `GET /queries`.

### Schema introspection as prompt engineering

The model has never seen the database. Everything it knows comes from a schema description introspected at startup and injected into the prompt:

```
Table: track
  track_id (integer)
  name (varchar)
  album_id (integer) -> album.album_id
  genre_id (integer) -> genre.genre_id
  unit_price (numeric)

Table: customer
  ...
  country (varchar)  e.g. 'USA', 'Canada', 'France'
```

Three deliberate choices:

- **Foreign keys as arrows** — introspected from `pg_catalog` (not `information_schema`, which is ownership-filtered and silently returns nothing to a non-owner role — see Known Limitations for the story). The arrows are why the model can write three-table joins it was never taught.
- **Sample values on categorical columns** — prevents the worst failure mode in text-to-SQL: `WHERE country = 'United States'` returning zero rows when the data stores `'USA'`. Valid SQL, no error, confidently wrong answer — the retry loop can't catch what doesn't fail. Columns are sampled only if they have ≤30 distinct values *and* repeat values heavily (distinct < 50% of rows), so `country` gets examples and `email` doesn't.
- **Compact custom format over raw DDL** — roughly a third of the tokens, resent on every call including retries, with the join paths as the most prominent element.

### Bounded retry with error feedback

On a SQL execution error, the failed query and the database's error text go back to the model as messages — it sees its own output and what reality said about it, then corrects. Capped at 2 retries.

Validation **blocks never retry**: an execution error is informative ("column `custmer_id` does not exist"), but a block means the model produced something forbidden — and if the question was adversarial, retrying invites a second attempt at the attack.

---

## API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/ask` | Question in, answer + SQL + rows + outcome out |
| `GET` | `/queries` | The audit log, newest first (`?limit=`, capped at 200) |
| `POST` | `/validate` | Feed SQL directly to the validator — returns the verdict, never executes |
| `GET` | `/health` | Liveness check that actually touches the database (503 if unreachable) |

Note: `numeric` columns (money) serialize as JSON strings (`"49.62"`) to preserve exact decimal values.

## Architecture

```
app/
├── main.py        # FastAPI endpoints, lifespan schema warm-up
├── agent.py       # generate → validate → execute → retry → explain
├── validator.py   # the safety layer (sqlparse, zero dependencies on DB/LLM)
├── schema.py      # introspection: columns, FK arrows, sample values (cached)
├── db.py          # two connections: read-only (untrusted SQL) + log (audit)
├── models.py      # Pydantic request/response contracts
└── config.py      # env + constants
```

Built as a **fixed pipeline with retry, not a tool-calling agent** — deliberately. For a single database with a known schema, the tool sequence is fixed in advance (inspect schema → write query → execute), so letting the model rediscover it every request costs 4–8 LLM calls instead of 2, plus debuggability, for no benefit. The retry loop is the one place model output drives control flow, and it's ReAct-shaped: error as observation, model corrects. Multi-database routing or unpredictable tool sequences would justify upgrading to tool-calling.

## Running locally

Requires Docker and an OpenAI API key.

```bash
git clone https://github.com/YOUR-USERNAME/sql-agent.git
cd sql-agent

# Postgres with Chinook + roles auto-loaded on first boot
docker compose up -d

# .env
cat > .env << 'EOF'
OPENAI_API_KEY=sk-your-key
AGENT_RO_DSN=host=localhost port=5433 dbname=chinook user=agent_ro password=ro_pass
AGENT_LOG_DSN=host=localhost port=5433 dbname=chinook user=agent_log password=log_pass
EOF

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Open `http://localhost:8080/docs`. Note: if you edit the SQL init files after first boot, `docker compose down -v` — init scripts only run on an empty volume.

Tests: `python tests/check_validator.py` runs the 18-case validator suite (no database or API key needed — the validator is deliberately pure). The 10-question type coverage against the live pipeline is documented in [`tests/test_questions.md`](tests/test_questions.md).

## Known limitations

Stated explicitly, because knowing the gaps matters as much as the features:

- **Data-modifying CTEs**: PostgreSQL permits `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`, which `sqlparse` types as SELECT. Rule 3 catches the DELETE keyword, and the role grants catch it regardless — but rule 2 alone would pass it. This is the sharpest known edge in the validator.
- **The validator does not check table access** — a `SELECT` against `query_log` passes validation and is stopped by role grants. Deliberate: table access is enforced at a stronger layer, and duplicating it would create two sources of truth. Documented by a test case (`UNION` against `query_log`) that *passes* validation by design.
- **No result-size bound** — a SELECT returning millions of rows passes every rule; only the 10s statement timeout limits it. A row cap at the executor would close this.
- **Denylist shape on rule 3** — rules 1–2 are allowlist-shaped (one statement, must be SELECT); the keyword list is a redundant layer, not the primary control.
- **Sample-value thresholds are unvalidated heuristics** (≤30 distinct, <50% ratio). A 31-value column that matters gets silently skipped.
- **Ambiguous questions are refused rather than clarified** — "show me the top performers" returns `unanswerable`. The pipeline has no clarification mechanism; the prompt-level fix (state an assumption, which the returned SQL automatically documents) is identified but not applied.
- **Schema cached at startup** — assumes schema changes coincide with deployments. True here; false with online migrations.
- **Railway connection uses the public endpoint** — private-network DNS (`postgres.railway.internal`) did not resolve reliably during deployment. Production would use private networking.
- **Credentials in env vars / init scripts** — fine for a demo database of public sample data; production would use IAM auth or a secrets manager.
- **No authentication or rate limiting on the API** — appropriate for a portfolio demo, not for exposure with a real database.
- **Nullable `track.album_id`**: inner joins from track→album→artist silently drop albumless tracks. Answers can look correct and be subtly wrong — which is exactly why the SQL is returned with every answer.

## What I'd measure next

Everything above is verified by hand against known-correct Chinook values. The correct next step is an evaluation set: question + gold-SQL pairs scored on **execution accuracy** (do the results match, not the strings), with failures broken down by category — schema misunderstanding vs join errors vs value-format mismatches — so investment goes to the right component.
