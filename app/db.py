from contextlib import asynccontextmanager
import psycopg
from psycopg import sql
from app.config import AGENT_LOG_DSN, AGENT_RO_DSN, STATEMENT_TIMEOUT

@asynccontextmanager
async def get_ro_conn():
    async with await psycopg.AsyncConnection.connect(
        AGENT_RO_DSN, autocommit=True
    ) as conn:
        await conn.set_read_only(True)
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("SET statement_timeout = {}").format(
                    sql.Literal(STATEMENT_TIMEOUT)
                )
            )
        yield conn

@asynccontextmanager
async def get_log_conn():
    async with await psycopg.AsyncConnection.connect(AGENT_LOG_DSN) as conn:
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
