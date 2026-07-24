from dataclasses import dataclass, field
from openai import AsyncOpenAI

from app.config import OPENAI_API_KEY, LLM_MODEL, MAX_RETRIES
from app.schema import get_schema_description
from app.validator import validate_sql
from app.db import get_ro_conn, get_log_conn

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


SQL_SYSTEM_PROMPT = """You write PostgreSQL SELECT queries against a music store database.

Rules:
- Output ONLY the SQL query. No explanation, no markdown fences, no commentary.
- SELECT statements only. Never write INSERT, UPDATE, DELETE, DROP, or any other statement type.
- Use only the tables and columns in the schema below. Never invent a column.
- Follow the arrows (->) in the schema to find join paths. Multi-table joins are expected and normal.
- When a column shows example values, match that exact format.
- Always add a LIMIT clause unless the question asks for an aggregate or a count.

Attempt every question about this music store data, however many joins it requires.
Only output CANNOT_ANSWER when the question is about a subject this database has no
data on at all (for example: philosophy, weather, current events), or when it asks
to modify data rather than read it.

Schema:
{schema}"""

EXPLAIN_SYSTEM_PROMPT = """You answer questions about a music store database.

You are given the user's question, the SQL that was run, and the rows it returned.
Answer using only those rows. Do not invent numbers.

- Single value or one row: answer in one plain sentence.
- A handful of rows: state them directly, as a short list.
- Many rows: give the headline finding in a sentence, then note that the full
  result set is available rather than summarising it vaguely.

If the result set is empty, say that no matching records were found."""

@dataclass
class AgentResult:
    question: str
    answer: str
    sql: str | None = None
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    attempts: int = 1
    outcome: str = "executed"     # 'executed' | 'blocked' | 'error' | 'unanswerable'
    block_reason: str | None = None
    error_message: str | None = None

async def generate_sql(question: str, schema: str, history: list | None = None) -> str:
    """One LLM call. Returns raw model output — not yet validated."""
    messages = [
        {"role": "system", "content": SQL_SYSTEM_PROMPT.format(schema=schema)},
        {"role": "user", "content": question},
    ]
    if history:
        messages.extend(history)

    response = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0,
    )
    return response.choices[0].message.content.strip()

async def run_query(sql: str) -> tuple[list, list]:
    """Execute validated SQL through the read-only connection."""
    async with get_ro_conn() as conn:
        cur = await conn.execute(sql)
        rows = await cur.fetchall()
        columns = [d.name for d in cur.description] if cur.description else []
    return rows, columns


async def answer_question(question: str) -> AgentResult:
    schema = await get_schema_description()
    history: list = []

    for attempt in range(1, MAX_RETRIES + 2):   # 1 initial + MAX_RETRIES
        raw_sql = await generate_sql(question, schema, history)
        # The model declined — not an error, a designed outcome.
        if raw_sql.strip().upper().startswith("CANNOT_ANSWER"):
            return AgentResult(
                question=question,
                answer="That question can't be answered from this database.",
                attempts=attempt,
                outcome="unanswerable",
            )

        # Safety gate. No retry on a block — a rejected query means the model
        # tried something forbidden, and asking again invites it to try harder.
        verdict = validate_sql(raw_sql)
        if not verdict.is_safe:
            return AgentResult(
                question=question,
                answer="That query was blocked by the safety layer and not executed.",
                sql=raw_sql,
                attempts=attempt,
                outcome="blocked",
                block_reason=verdict.reason,
            )

        sql = verdict.cleaned_sql

        try:
            rows, columns = await run_query(sql)
        except Exception as exc:
            error_text = str(exc).strip()

            # Out of retries — give up and report honestly.
            if attempt > MAX_RETRIES:
                return AgentResult(
                    question=question,
                    answer=f"Couldn't produce a working query after {attempt} attempts.",
                    sql=sql,
                    attempts=attempt,
                    outcome="error",
                    error_message=error_text,
                )

            # Feed the error back as an observation and let the model correct.
            history.extend([
                {"role": "assistant", "content": sql},
                {"role": "user", "content":
                    f"That query failed with this database error:\n{error_text}\n"
                    "Fix the query. Output only the corrected SQL."},
            ])
            continue

        answer = await explain_results(question, sql, rows, columns)
        return AgentResult(
            question=question,
            answer=answer,
            sql=sql,
            rows=rows,
            columns=columns,
            attempts=attempt,
            outcome="executed",
        )
        

async def explain_results(question: str, sql: str, rows: list, columns: list) -> str:
    preview = rows[:50]
    payload = (
        f"Question: {question}\n\n"
        f"SQL:\n{sql}\n\n"
        f"Columns: {columns}\n"
        f"Rows ({len(rows)} total, showing up to 50):\n{preview}"
    )
    response = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        temperature=0,
    )
    return response.choices[0].message.content.strip()


async def log_result(result: AgentResult) -> None:
    """Write to the audit trail on the separate log connection."""
    async with get_log_conn() as conn:
        await conn.execute(
            """
            INSERT INTO query_log
                (question, generated_sql, outcome, block_reason, error_message)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                result.question,
                result.sql,
                result.outcome,
                result.block_reason,
                result.error_message,
            ),
        )