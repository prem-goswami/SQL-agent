import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.agent import answer_question, log_result
from app.schema import get_schema_description
from app.db import get_log_conn, get_ro_conn
from app.validator import validate_sql
from app.models import AskRequest, AskResponse, QueryLogEntry, QueryLogResponse, ValidateRequest

# psycopg3 cannot use Windows' default ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@asynccontextmanager
async def lifespan(app: FastAPI):
    schema = await get_schema_description()
    table_count = schema.count("Table: ")
    print(f"[Startup] Schema cached — {table_count} tables.")
    yield
    print("[Shutdown] Done.")


app = FastAPI(
    title="SQL Agent",
    description="Natural-language questions against a PostgreSQL database, "
                "with a deterministic SELECT-only safety layer.",
    version="1.0.0",
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
        return {"status": "healthy", "database": "reachable"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {exc}")

@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    result = await answer_question(request.question)

    try:
        await log_result(result)
    except Exception as exc:
        # A logging failure must not break a working answer.
        print(f"[Warning] Could not write to query_log: {exc}")

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
        error_message=result.error_message,
    )


@app.get("/queries", response_model=QueryLogResponse)
async def queries(limit: int = 50):
    limit = max(1, min(limit, 200))
    async with get_log_conn() as conn:
        cur = await conn.execute(
            """
            SELECT id, created_at, question, generated_sql,
                   outcome, block_reason, error_message
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
            id=r[0], created_at=r[1], question=r[2], generated_sql=r[3],
            outcome=r[4], block_reason=r[5], error_message=r[6],
        )
        for r in rows
    ]
    return QueryLogResponse(entries=entries, total=total)

@app.post("/validate")
async def validate_only(request: ValidateRequest):
    """Validation-layer demonstration. Never executes — parses and returns the verdict."""
    verdict = validate_sql(request.sql)
    return {
        "sql": request.sql,
        "is_safe": verdict.is_safe,
        "reason": verdict.reason,
        "cleaned_sql": verdict.cleaned_sql,
    }
