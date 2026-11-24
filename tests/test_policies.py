import asyncio
import json

import pytest

import app.agent as agent
from app.policies import PolicyError, check_against_schema, load_policy_file, parse_policy
from app.schema import render_schema

SCHEMA = {
    "artist": [("artist_id", "  artist_id (integer)", None), ("name", "  name (varchar)", None)],
    "album": [("album_id", "  album_id (integer)", None), ("title", "  title (varchar)", None),
              ("artist_id", "  artist_id (integer) -> artist.artist_id", "artist")],
    "customer": [("customer_id", "  customer_id (integer)", None), ("email", "  email (varchar)", None)],
}
COLUMNS = {t: {c[0] for c in cols} for t, cols in SCHEMA.items()}


def test_example_policies_are_valid():
    for path in ("policies/support_no_pii.json", "policies/analyst_brazil_canada.json"):
        p = load_policy_file(path)
        assert p.name and (p.columns or p.rows or p.tables)


def test_round_trip():
    doc = {"name": "x", "tables": ["album", "artist"], "columns": {"artist": {"deny": ["name"]}},
           "rows": {"album": {"artist_id": [1, 2]}}}
    assert parse_policy(parse_policy(doc).to_json()) == parse_policy(doc)


@pytest.mark.parametrize("doc", [
    {"tables": ["artist"]},                                         # no name
    {"name": "x", "views": ["artist"]},                             # unknown key
    {"name": "x", "tables": []},                                    # empty list
    {"name": "x", "columns": {"artist": ["name"]}},                 # not {"deny": [...]}
    {"name": "x", "rows": {"album": {"artist_id": []}}},            # no allowed values
    {"name": "x", "rows": {"album": {"artist_id": [True]}}},        # booleans are not ids
    {"name": "x", "tables": ["artist"], "columns": {"album": {"deny": ["title"]}}},  # rule outside tables
])
def test_bad_documents_are_rejected(doc):
    with pytest.raises(PolicyError):
        parse_policy(doc)


def test_policy_checked_against_schema():
    check_against_schema(parse_policy({"name": "ok", "columns": {"customer": {"deny": ["email"]}}}), COLUMNS)
    with pytest.raises(PolicyError):
        check_against_schema(parse_policy({"name": "x", "tables": ["invoices"]}), COLUMNS)
    with pytest.raises(PolicyError):
        check_against_schema(parse_policy({"name": "x", "columns": {"customer": {"deny": ["mail"]}}}), COLUMNS)


def test_schema_text_limited_to_visible_tables():
    text = render_schema(SCHEMA, frozenset({"album"}))
    assert "Table: album" in text and "Table: artist" not in text and "Table: customer" not in text
    assert "-> artist" not in text            # no arrow into a hidden table
    assert "Table: customer" in render_schema(SCHEMA)


def test_agent_shows_only_policy_tables(monkeypatch):
    seen = {}
    async def schema(visible=None):
        seen["visible"] = visible
        return "Table: album"
    async def gen(q, s, h=None, context=None):
        return "CANNOT_ANSWER"
    monkeypatch.setattr(agent, "get_schema_description", schema)
    monkeypatch.setattr(agent, "generate_sql", gen)
    policy = parse_policy({"name": "x", "tables": ["album"]})
    asyncio.run(agent.answer_question("q", policy=policy))
    assert seen["visible"] == frozenset({"album"})
    asyncio.run(agent.answer_question("q"))
    assert seen["visible"] is None
