"""Conversations: a session keeps earlier turns so follow-up questions have context."""
import secrets
from datetime import datetime, timedelta, timezone

from app.config import SESSION_CONTEXT_TURNS, SESSION_TTL_HOURS
from app.db import get_log_conn


async def create_session(key_id: int) -> dict:
    sid = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=SESSION_TTL_HOURS)
    async with get_log_conn() as conn:
        await conn.execute(
            "INSERT INTO sessions (id, key_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (sid, key_id, now, exp))
    return {"session_id": sid, "created_at": now, "expires_at": exp}


async def get_session(sid: str, key_id: int):
    """Session row plus turns, or None if missing, expired or owned by another key."""
    async with get_log_conn() as conn:
        cur = await conn.execute(
            "SELECT id, created_at, expires_at FROM sessions WHERE id = %s AND key_id = %s AND expires_at > now()",
            (sid, key_id))
        row = await cur.fetchone()
        if row is None:
            return None
        cur = await conn.execute(
            "SELECT question, sql, outcome, answer, created_at FROM session_turns "
            "WHERE session_id = %s ORDER BY id", (sid,))
        turns = [dict(question=t[0], sql=t[1], outcome=t[2], answer=t[3], created_at=t[4])
                 for t in await cur.fetchall()]
    return {"session_id": row[0], "created_at": row[1], "expires_at": row[2], "turns": turns}


async def add_turn(sid: str, result) -> None:
    async with get_log_conn() as conn:
        await conn.execute(
            "INSERT INTO session_turns (session_id, question, sql, outcome, answer) VALUES (%s, %s, %s, %s, %s)",
            (sid, result.question, result.sql, result.outcome, result.answer))


def context_from(session: dict) -> list:
    return session["turns"][-SESSION_CONTEXT_TURNS:]
