import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.agent import AUDIT_STATUS, answer_question, check_audit_log, fetch_page, log_result
from app.auth import Caller, admin_only, current_caller, rate_limited
from app import sessions as sess
from app.schema import get_schema_description
from app.db import get_log_conn, get_ro_conn
from app.validator import validate_sql
from app.models import (
    AskRequest,
    AskResponse,
    QueryLogEntry,
    QueryLogResponse,
    SessionResponse,
    ValidateRequest,
)

# psycopg3 cannot use Windows' default ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@asynccontextmanager
async def lifespan(app: FastAPI):
    schema = await get_schema_description()
    table_count = schema.count("Table: ")
    print(f"[Startup] Schema cached — {table_count} tables.")
    status = await check_audit_log()
    if status != "ok":
        print(f"[Startup] Audit log: {status}")
    yield
    print("[Shutdown] Done.")


app = FastAPI(
    title="SQL Agent",
    description="Natural-language questions against a PostgreSQL database, "
    "with a deterministic SELECT-only safety layer.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    try:
        async with get_ro_conn() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {exc}")
    audit = await check_audit_log()
    return {"status": "healthy" if audit == "ok" else "degraded", "database": "reachable", "audit_log": audit}


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest, caller: Caller = Depends(rate_limited)):
    context = None
    if request.session_id is not None:
        session = await sess.get_session(request.session_id, caller.key_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found or expired")
        context = sess.context_from(session)

    result = await answer_question(request.question, context=context, owner=caller.key_id, policy=caller.policy)

    try:
        await log_result(result)
    except Exception as exc:
        # A logging failure must not break a working answer.
        print(f"[Warning] Could not write to query_log: {exc}")
    if request.session_id is not None:
        await sess.add_turn(request.session_id, result)

    return AskResponse(
        question=result.question,
        answer=result.answer,
        sql=result.sql,
        columns=result.columns,
        rows=[list(r) for r in result.rows],
        row_count=len(result.rows),
        attempts=result.attempts,
        outcome=result.outcome,
        block_reason=result.block_reason,
        block_rule=result.block_rule,
        error_message=result.error_message,
        truncated=result.truncated,
        next_cursor=result.next_cursor,
        session_id=request.session_id,
    )


@app.get("/rows")
async def rows(cursor: str, caller: Caller = Depends(rate_limited)):
    page = await fetch_page(cursor, caller.key_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Unknown or expired cursor")
    return page


@app.get("/keys/me")
async def whoami(caller: Caller = Depends(current_caller)):
    """The calling key's id, role and policy, so a client can see what it is allowed."""
    return {"key_id": caller.key_id, "role": caller.role,
            "policy": caller.policy.to_json() if caller.policy else None}


@app.post("/sessions")
async def create_session(caller: Caller = Depends(current_caller)):
    return await sess.create_session(caller.key_id)


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def read_session(session_id: str, caller: Caller = Depends(current_caller)):
    session = await sess.get_session(session_id, caller.key_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return session


@app.get("/queries", response_model=QueryLogResponse)
async def queries(limit: int = 50, caller: Caller = Depends(admin_only)):
    limit = max(1, min(limit, 200))
    has_rule = AUDIT_STATUS.get("block_rule_column") == "ok" or await check_audit_log() == "ok"
    rule_col = "block_rule" if has_rule else "NULL"
    async with get_log_conn() as conn:
        cur = await conn.execute(
            f"""
            SELECT id, created_at, question, generated_sql,
                   outcome, block_reason, error_message, {rule_col}
            FROM query_log
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = await cur.fetchall()

        cur = await conn.execute("SELECT COUNT(*) FROM query_log")
        (total,) = await cur.fetchone()

    entries = [
        QueryLogEntry(
            id=r[0],
            created_at=r[1],
            question=r[2],
            generated_sql=r[3],
            outcome=r[4],
            block_reason=r[5],
            error_message=r[6],
            block_rule=r[7],
        )
        for r in rows
    ]
    return QueryLogResponse(entries=entries, total=total)


@app.post("/validate")
async def validate_only(request: ValidateRequest, caller: Caller = Depends(rate_limited)):
    """Validation-only endpoint. Never executes — parses and returns the verdict."""
    verdict = validate_sql(request.sql)
    return {
        "sql": request.sql,
        "is_safe": verdict.is_safe,
        "reason": verdict.reason,
        "cleaned_sql": verdict.cleaned_sql,
        "rule": verdict.rule,
    }
