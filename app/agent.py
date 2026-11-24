import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import psycopg
from openai import AsyncOpenAI

from app.config import OPENAI_API_KEY, LLM_MODEL, MAX_RETRIES, ROW_CAP
from app.policies import Policy
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
If the question could reasonably mean different things, output CLARIFY: followed by one short question.
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

# Paging cursors: token -> (sql, offset, key_id, created). In memory, bounded, short-lived.
CURSORS: "OrderedDict[str, tuple]" = OrderedDict()
CURSOR_TTL_SECONDS = 15 * 60
MAX_CURSORS = 1000
AUDIT_STATUS = {"block_rule_column": "unchecked"}


@dataclass
class AgentResult:
    question: str
    answer: str
    sql: str | None = None
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    attempts: int = 1
    outcome: str = "executed"  # executed | blocked | error | unanswerable | needs_clarification
    block_reason: str | None = None
    block_rule: str | None = None
    error_message: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    session_id: str | None = None


def _context_messages(context):
    msgs = []
    for t in context or []:
        msgs.append({"role": "user", "content": t["question"]})
        reply = t.get("sql") or t.get("answer") or ""
        msgs.append({"role": "assistant", "content": reply})
    return msgs


async def generate_sql(question: str, schema: str, history: list | None = None, context: list | None = None) -> str:
    """One LLM call. Returns raw model output — not yet validated."""
    messages = [{"role": "system", "content": SQL_SYSTEM_PROMPT.format(schema=schema)}]
    messages += _context_messages(context)
    messages.append({"role": "user", "content": question})
    if history:
        messages.extend(history)
    response = await client.chat.completions.create(model=LLM_MODEL, messages=messages, temperature=0)
    return response.choices[0].message.content.strip()


def _page_sql(sql: str, offset: int) -> str:
    inner = sql.strip().rstrip(";")
    return f"SELECT * FROM ({inner}) AS page_q LIMIT {ROW_CAP + 1} OFFSET {int(offset)}"


async def run_query(sql: str, offset: int = 0) -> tuple[list, list]:
    """Execute validated SQL through the read-only connection, at most ROW_CAP + 1 rows."""
    async with get_ro_conn() as conn:
        cur = await conn.execute(_page_sql(sql, offset))
        rows = await cur.fetchall()
        columns = [d.name for d in cur.description] if cur.description else []
    return rows, columns


def _cursor_for(sql: str, offset: int, owner) -> str:
    token = secrets.token_urlsafe(16)
    CURSORS[token] = (sql, offset, owner, time.monotonic())
    while len(CURSORS) > MAX_CURSORS:
        CURSORS.popitem(last=False)
    return token


async def fetch_page(cursor: str, owner):
    """Next page for a cursor issued to this key; None if unknown, expired or not theirs."""
    sql, offset, who, created = CURSORS.get(cursor, (None, None, None, 0.0))
    if sql is None or who != owner or time.monotonic() - created > CURSOR_TTL_SECONDS:
        return None
    verdict = validate_sql(sql)
    if not verdict.is_safe:
        return None
    rows, columns = await run_query(sql, offset)
    truncated = len(rows) > ROW_CAP
    rows = rows[:ROW_CAP]
    nxt = _cursor_for(sql, offset + ROW_CAP, owner) if truncated else None
    return {"rows": [list(r) for r in rows], "columns": columns, "row_count": len(rows),
            "truncated": truncated, "next_cursor": nxt}


async def answer_question(question: str, context: list | None = None, owner=None,
                          policy: Policy | None = None) -> AgentResult:
    # The policy's table list shapes what the model is shown; see app/policies.py.
    schema = await get_schema_description(policy.tables if policy else None)
    history: list = []
    for attempt in range(1, MAX_RETRIES + 2):   # 1 initial + MAX_RETRIES
        raw_sql = await generate_sql(question, schema, history, context)
        text = raw_sql.strip()
        if text.upper().startswith("CANNOT_ANSWER"):
            return AgentResult(question=question, answer="That question can't be answered from this database.",
                               attempts=attempt, outcome="unanswerable")
        if text.upper().startswith("CLARIFY:"):
            return AgentResult(question=question, answer=text[len("CLARIFY:"):].strip(),
                               attempts=attempt, outcome="needs_clarification")
        verdict = validate_sql(raw_sql)
        if not verdict.is_safe:
            return AgentResult(question=question, answer="That query was blocked by the safety layer and not executed.",
                               sql=raw_sql, attempts=attempt, outcome="blocked",
                               block_reason=verdict.reason, block_rule=verdict.rule)
        sql = verdict.cleaned_sql
        try:
            rows, columns = await run_query(sql)
        except Exception as exc:
            error_text = str(exc).strip()
            if attempt > MAX_RETRIES:
                return AgentResult(question=question, answer=f"Couldn't produce a working query after {attempt} attempts.",
                                   sql=sql, attempts=attempt, outcome="error", error_message=error_text)
            history.extend([
                {"role": "assistant", "content": sql},
                {"role": "user", "content": f"That query failed with this database error:\n{error_text}\n"
                                            "Fix the query. Output only the corrected SQL."},
            ])
            continue
        truncated = len(rows) > ROW_CAP
        rows = rows[:ROW_CAP]
        answer = await explain_results(question, sql, rows, columns, truncated)
        return AgentResult(question=question, answer=answer, sql=sql, rows=rows, columns=columns,
                           attempts=attempt, outcome="executed", truncated=truncated,
                           next_cursor=_cursor_for(sql, ROW_CAP, owner) if truncated else None)


async def explain_results(question: str, sql: str, rows: list, columns: list, truncated: bool = False) -> str:
    preview = rows[:50]
    payload = (f"Question: {question}\n\nSQL:\n{sql}\n\nColumns: {columns}\n"
               f"Rows ({len(rows)} shown, showing up to 50):\n{preview}")
    if truncated:
        payload += f"\n\nOnly the first {len(rows)} rows are shown; say the list is not complete."
    response = await client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": EXPLAIN_SYSTEM_PROMPT}, {"role": "user", "content": payload}],
        temperature=0)
    return response.choices[0].message.content.strip()


async def check_audit_log() -> str:
    """Report whether query_log has block_rule. Never raises."""
    try:
        async with get_log_conn() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name = 'query_log' AND column_name = 'block_rule'")
            status = "ok" if await cur.fetchone() else "missing block_rule column: run sql/03_query_log_block_rule.sql"
    except Exception as exc:
        status = f"unknown: {type(exc).__name__}"
    AUDIT_STATUS["block_rule_column"] = status
    return status


async def log_result(result: AgentResult) -> None:
    """Write to the audit trail on the separate log connection."""
    try:
        async with get_log_conn() as conn:
            await conn.execute(
                "INSERT INTO query_log (question, generated_sql, outcome, block_reason, block_rule, error_message) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (result.question, result.sql, result.outcome, result.block_reason, result.block_rule, result.error_message))
        AUDIT_STATUS["block_rule_column"] = "ok"
    except psycopg.errors.UndefinedColumn:
        AUDIT_STATUS["block_rule_column"] = "missing block_rule column: run sql/03_query_log_block_rule.sql"
        async with get_log_conn() as conn:
            await conn.execute(
                "INSERT INTO query_log (question, generated_sql, outcome, block_reason, error_message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (result.question, result.sql, result.outcome, result.block_reason, result.error_message))
