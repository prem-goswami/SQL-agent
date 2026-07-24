from app.db import get_ro_conn
from psycopg import sql

_schema_cache: str | None = None

# Simplifies wordy PostgreSQL data types into standard short aliases to save prompt tokens
TYPE_MAP = {
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "double precision": "float",
}

COLUMNS_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name != 'query_log'
ORDER BY table_name, ordinal_position;
"""

# Queries pg_catalog directly to bypass information_schema ownership restrictions for agent_ro.
# NOTE: conkey[1] / confkey[1] extracts the first column of each FK constraint.
# This assumes single-column foreign keys, which covers 100% of the Chinook schema.
FK_SQL = """
SELECT
    src.relname     AS table_name,
    src_col.attname AS column_name,
    tgt.relname     AS ref_table,
    tgt_col.attname AS ref_column
FROM pg_constraint c
JOIN pg_class src ON src.oid = c.conrelid
JOIN pg_class tgt ON tgt.oid = c.confrelid
JOIN pg_namespace n ON n.oid = src.relnamespace
JOIN pg_attribute src_col
  ON src_col.attrelid = c.conrelid AND src_col.attnum = c.conkey[1]
JOIN pg_attribute tgt_col
  ON tgt_col.attrelid = c.confrelid AND tgt_col.attnum = c.confkey[1]
WHERE c.contype = 'f'
  AND n.nspname = 'public';
"""
SAMPLE_TARGETS_SQL = """
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name != 'query_log'
  AND data_type IN ('character varying', 'text')
ORDER BY table_name, ordinal_position;
""" 

async def build_sample_map(conn, max_distinct: int = 30) -> dict:
    """
    For each text column: count distinct values. If few enough,
    grab 3 examples. Returns {(table, column): "'USA', 'Canada', 'France'"}
    """
    cur = await conn.execute(SAMPLE_TARGETS_SQL)
    targets = await cur.fetchall()

    samples = {}
    for table, col in targets:
        count_q = sql.SQL("SELECT COUNT(DISTINCT {col}) FROM {tbl}").format(
            col=sql.Identifier(col), tbl=sql.Identifier(table)
        )
        cur = await conn.execute(count_q)
        (distinct_count,) = await cur.fetchone()

        if distinct_count == 0 or distinct_count > max_distinct:
            continue

        sample_q = sql.SQL(
            "SELECT DISTINCT {col} FROM {tbl} WHERE {col} IS NOT NULL LIMIT 3"
        ).format(col=sql.Identifier(col), tbl=sql.Identifier(table))
        cur = await conn.execute(sample_q)
        values = [r[0] for r in await cur.fetchall()]
        if values:
            samples[(table, col)] = ", ".join(f"'{v}'" for v in values)
    # print(f"[DEBUG] sampled {len(samples)} columns: {list(samples.keys())[:5]}")
    return samples

async def build_schema_description() -> str:
    async with get_ro_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(COLUMNS_SQL)
            columns = await cur.fetchall()

            await cur.execute(FK_SQL)
            fks = await cur.fetchall()
            
            samples = await build_sample_map(conn)

    # (table, column) -> "ref_table.ref_column"
    fk_map = {
        (table, col): f"{ref_table}.{ref_col}"
        for table, col, ref_table, ref_col in fks
    }

    # Group columns by table, preserving ordinal order
    tables: dict[str, list[str]] = {}
    for table, col, raw_dtype in columns:
        dtype = TYPE_MAP.get(raw_dtype, raw_dtype)
        line = f"  {col} ({dtype})"
        ref = fk_map.get((table, col))
        if ref:
            line += f" -> {ref}"
        ex = samples.get((table, col))
        if ex:
            line += f"  e.g. {ex}"
        tables.setdefault(table, []).append(line)

    blocks = []
    for table, lines in tables.items():
        blocks.append(f"Table: {table}\n" + "\n".join(lines))

    return "\n\n".join(blocks)


async def get_schema_description() -> str:
    """Cached accessor — builds once, reuses thereafter."""
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = await build_schema_description()
    return _schema_cache

