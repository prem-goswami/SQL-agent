from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    session_id: str | None = None


class AskResponse(BaseModel):
    question: str
    answer: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    attempts: int = 1
    outcome: str
    block_reason: str | None = None
    block_rule: str | None = None
    error_message: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    session_id: str | None = None


class QueryLogEntry(BaseModel):
    id: int
    created_at: datetime
    question: str
    generated_sql: str | None = None
    outcome: str
    block_reason: str | None = None
    block_rule: str | None = None
    error_message: str | None = None


class QueryLogResponse(BaseModel):
    entries: list[QueryLogEntry]
    total: int


class ValidateRequest(BaseModel):
    sql: str


class SessionTurn(BaseModel):
    question: str
    sql: str | None = None
    outcome: str
    answer: str
    created_at: datetime


class SessionResponse(BaseModel):
    session_id: str
    created_at: datetime
    expires_at: datetime
    turns: list[SessionTurn] = []
