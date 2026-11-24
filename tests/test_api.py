import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.main as main
from app.agent import AgentResult

from app.policies import parse_policy

SCOPED = parse_policy({"name": "scoped", "tables": ["artist", "album"]})
KEYS = {"admin-key": (1, "admin", None), "user-key": (2, "user", None), "other-key": (3, "user", None),
        "scoped-key": (4, "user", SCOPED)}


@pytest.fixture
def client(monkeypatch):
    async def lookup(k): return KEYS.get(k)
    seen = {}
    async def answer(q, context=None, owner=None, policy=None):
        seen["policy"] = policy
        return AgentResult(question=q, answer="fine", sql="SELECT 1", outcome="executed")
    async def log(r): pass
    store = {}
    async def create(key_id):
        store["s1"] = key_id
        return {"session_id": "s1", "created_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-02T00:00:00Z"}
    async def get(sid, key_id):
        if store.get(sid) != key_id: return None
        return {"session_id": sid, "created_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-02T00:00:00Z", "turns": []}
    async def add(sid, r): pass
    monkeypatch.setattr(auth, "lookup", lookup)
    monkeypatch.setattr(main, "answer_question", answer)
    monkeypatch.setattr(main, "log_result", log)
    monkeypatch.setattr(main.sess, "create_session", create)
    monkeypatch.setattr(main.sess, "get_session", get)
    monkeypatch.setattr(main.sess, "add_turn", add)
    auth._hits.clear()
    c = TestClient(main.app)  # no lifespan: nothing touches a database
    c.seen = seen
    return c


def test_missing_and_bad_key(client):
    assert client.post("/ask", json={"question": "hello"}).status_code == 401
    assert client.post("/ask", json={"question": "hello"}, headers={"X-API-Key": "nope"}).status_code == 401


def test_user_cannot_read_audit_log(client):
    assert client.get("/queries", headers={"X-API-Key": "user-key"}).status_code == 403


def test_rate_limit_is_per_key(client, monkeypatch):
    monkeypatch.setattr(auth, "RATE_LIMIT_REQUESTS", 2)
    u = {"X-API-Key": "user-key"}
    codes = [client.post("/validate", json={"sql": "SELECT 1"}, headers=u).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.post("/validate", json={"sql": "SELECT 1"}, headers={"X-API-Key": "other-key"}).status_code == 200


def test_validate_returns_rule(client):
    body = client.post("/validate", json={"sql": "SELECT pg_sleep(1)"}, headers={"X-API-Key": "user-key"}).json()
    assert body["is_safe"] is False and body["rule"] == "function"


def test_session_is_private_to_its_key(client):
    sid = client.post("/sessions", headers={"X-API-Key": "user-key"}).json()["session_id"]
    assert client.get(f"/sessions/{sid}", headers={"X-API-Key": "user-key"}).status_code == 200
    assert client.get(f"/sessions/{sid}", headers={"X-API-Key": "other-key"}).status_code == 404
    r = client.post("/ask", json={"question": "hello", "session_id": sid}, headers={"X-API-Key": "other-key"})
    assert r.status_code == 404


def test_policy_reaches_the_agent(client):
    client.post("/ask", json={"question": "hello"}, headers={"X-API-Key": "scoped-key"})
    assert client.seen["policy"] is SCOPED
    me = client.get("/keys/me", headers={"X-API-Key": "scoped-key"}).json()
    assert me["role"] == "user" and me["policy"]["tables"] == ["album", "artist"]
