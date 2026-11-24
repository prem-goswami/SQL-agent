import asyncio
import pytest
import app.agent as agent


@pytest.fixture
def fakes(monkeypatch):
    st = {"llm": [], "ran": [], "errors": [], "ctx": []}
    async def schema(visible=None): return "Table: artist"
    async def gen(q, s, h=None, context=None):
        st["ctx"].append(context)
        return st["llm"].pop(0)
    async def run(sql, offset=0):
        st["ran"].append((sql, offset))
        if st["errors"]: raise RuntimeError(st["errors"].pop(0))
        return [(i,) for i in range(st.get("nrows", 1))], ["n"]
    async def explain(*a, **k): return "ok"
    monkeypatch.setattr(agent, "get_schema_description", schema)
    monkeypatch.setattr(agent, "generate_sql", gen)
    monkeypatch.setattr(agent, "run_query", run)
    monkeypatch.setattr(agent, "explain_results", explain)
    return st


def test_blocked_query_never_runs(fakes):
    fakes["llm"] = ["SELECT 1; DROP TABLE customer", "SELECT 1"]
    r = asyncio.run(agent.answer_question("q"))
    assert r.outcome == "blocked" and r.block_rule == "single_statement" and fakes["ran"] == []


def test_unanswerable(fakes):
    fakes["llm"] = ["CANNOT_ANSWER"]
    assert asyncio.run(agent.answer_question("q")).outcome == "unanswerable"


def test_clarification(fakes):
    fakes["llm"] = ["CLARIFY: Which year do you mean?"]
    r = asyncio.run(agent.answer_question("q"))
    assert r.outcome == "needs_clarification" and r.answer == "Which year do you mean?" and fakes["ran"] == []


def test_db_error_retries_up_to_limit(fakes):
    n = agent.MAX_RETRIES + 1
    fakes["llm"] = ["SELECT bad"] * n
    fakes["errors"] = ["boom"] * n
    r = asyncio.run(agent.answer_question("q"))
    assert r.outcome == "error" and r.attempts == n


def test_row_cap_sets_cursor(fakes):
    fakes["llm"] = ["SELECT n FROM t"]
    fakes["nrows"] = agent.ROW_CAP + 1
    r = asyncio.run(agent.answer_question("q", owner=7))
    assert r.truncated and len(r.rows) == agent.ROW_CAP and r.next_cursor in agent.CURSORS


def test_session_context_reaches_llm(fakes):
    fakes["llm"] = ["SELECT 1"]
    ctx = [{"question": "first", "sql": "SELECT name FROM artist"}]
    asyncio.run(agent.answer_question("and then?", context=ctx))
    assert fakes["ctx"][0] == ctx
