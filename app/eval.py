"""Accuracy harness: python -m app.eval [--use-gold] [--cases eval/gold_cases.json]

Each case has a question and a gold SQL query. The candidate SQL (from the LLM,
or the gold SQL itself with --use-gold) goes through the validator and runs on
the read-only connection; a case passes when its rows match the gold rows as a
multiset. The last line printed is JSON: {"cases": N, "passed": P, "accuracy": P/N}.
"""
import argparse
import asyncio
import json
import sys
from collections import Counter
from decimal import Decimal

from app.db import get_ro_conn
from app.schema import get_schema_description
from app.validator import validate_sql

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _norm(v):
    if isinstance(v, (Decimal, float)):
        return round(float(v), 4)
    return v


async def _rows(sql: str):
    async with get_ro_conn() as conn:
        cur = await conn.execute(sql)
        return Counter(tuple(_norm(v) for v in r) for r in await cur.fetchall())


async def run(cases_path: str, use_gold: bool) -> dict:
    cases = json.load(open(cases_path))
    schema = None if use_gold else await get_schema_description()
    passed = 0
    for case in cases:
        if use_gold:
            candidate = case["gold_sql"]
        else:
            from app.agent import generate_sql
            candidate = await generate_sql(case["question"], schema)
        verdict = validate_sql(candidate)
        ok, note = False, ""
        if not verdict.is_safe:
            note = f"blocked ({verdict.rule})"
        else:
            try:
                ok = await _rows(verdict.cleaned_sql) == await _rows(case["gold_sql"])
                note = "match" if ok else "different rows"
            except Exception as exc:
                note = f"error: {type(exc).__name__}"
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {case['id']}: {case['question']} — {note}")
    return {"cases": len(cases), "passed": passed, "accuracy": round(passed / len(cases), 4) if cases else 0.0}


def main():
    p = argparse.ArgumentParser(prog="python -m app.eval")
    p.add_argument("--use-gold", action="store_true", help="score the gold SQL itself (no LLM calls)")
    p.add_argument("--cases", default="eval/gold_cases.json")
    a = p.parse_args()
    print(json.dumps(asyncio.run(run(a.cases, a.use_gold))))


if __name__ == "__main__":
    main()
