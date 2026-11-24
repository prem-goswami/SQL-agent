"""API key management.

    python -m app.keys create --role admin|user [--policy policies/<file>.json]
    python -m app.keys list
    python -m app.keys revoke <key_id>

`create` prints the new key once; only its SHA-256 hash is stored.
"""
import argparse
import asyncio
import hashlib
import secrets
import sys

from psycopg.types.json import Jsonb

from app.db import get_log_conn
from app.policies import Policy, PolicyError, check_against_schema, load_policy_file, parse_policy

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_key(role: str, policy: Policy | None = None) -> str:
    key = "sk_" + secrets.token_urlsafe(24)
    async with get_log_conn() as conn:
        await conn.execute(
            "INSERT INTO api_keys (key_hash, role, policy) VALUES (%s, %s, %s)",
            (hash_key(key), role, Jsonb(policy.to_json()) if policy else None),
        )
    return key


async def lookup(key: str):
    """(key_id, role, Policy | None) for an active key, else None."""
    async with get_log_conn() as conn:
        cur = await conn.execute(
            "SELECT id, role, policy FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
            (hash_key(key),),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    key_id, role, doc = row
    return key_id, role, parse_policy(doc) if doc else None


async def list_keys() -> list[tuple]:
    async with get_log_conn() as conn:
        cur = await conn.execute(
            "SELECT id, role, policy->>'name', created_at, revoked_at FROM api_keys ORDER BY id"
        )
        return await cur.fetchall()


async def revoke_key(key_id: int) -> bool:
    async with get_log_conn() as conn:
        cur = await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,)
        )
        return cur.rowcount == 1


async def _create(role: str, policy_path: str | None) -> str:
    policy = None
    if policy_path:
        from app.schema import get_table_columns
        policy = load_policy_file(policy_path)
        check_against_schema(policy, await get_table_columns())
    return await create_key(role, policy)


def main():
    p = argparse.ArgumentParser(prog="python -m app.keys")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create a key and print it")
    c.add_argument("--role", choices=["admin", "user"], required=True)
    c.add_argument("--policy", help="path to a policy JSON file")
    sub.add_parser("list", help="list keys (never shows the key itself)")
    r = sub.add_parser("revoke", help="revoke a key by id")
    r.add_argument("key_id", type=int)
    a = p.parse_args()

    if a.cmd == "create":
        try:
            print(asyncio.run(_create(a.role, a.policy)))
        except PolicyError as exc:
            sys.exit(f"policy rejected: {exc}")
    elif a.cmd == "list":
        for key_id, role, policy_name, created, revoked in asyncio.run(list_keys()):
            state = f"revoked {revoked:%Y-%m-%d}" if revoked else "active"
            print(f"{key_id}\t{role}\t{policy_name or '-'}\t{created:%Y-%m-%d}\t{state}")
    elif a.cmd == "revoke":
        if not asyncio.run(revoke_key(a.key_id)):
            sys.exit(f"no active key with id {a.key_id}")
        print(f"revoked key {a.key_id}")


if __name__ == "__main__":
    main()
