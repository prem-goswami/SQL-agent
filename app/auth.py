"""X-API-Key authentication, roles and per-key rate limiting."""
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app.config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS
from app.keys import lookup
from app.policies import Policy

_hits: dict = defaultdict(deque)


@dataclass
class Caller:
    key_id: int
    role: str
    policy: Policy | None = None


async def current_caller(x_api_key: str | None = Header(default=None)) -> Caller:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    found = await lookup(x_api_key)
    if found is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    key_id, role, policy = found
    return Caller(key_id=key_id, role=role, policy=policy)


async def rate_limited(caller: Caller = Depends(current_caller)) -> Caller:
    """Sliding window per API key on the endpoints that cost CPU or LLM calls."""
    now = time.monotonic()
    q = _hits[caller.key_id]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for this API key")
    q.append(now)
    return caller


async def admin_only(caller: Caller = Depends(current_caller)) -> Caller:
    if caller.role != "admin":
        raise HTTPException(status_code=403, detail="Admin key required")
    return caller
