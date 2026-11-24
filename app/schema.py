from app.db import get_ro_conn
from psycopg import sql

# Tables the service keeps for itself; never described to the model.
INTERNAL_TABLES = ("query_log", "api_keys", "sessions", "session_turns")

_schema_cache: dict | None = None

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
  AND table_name <> ALL(%s)
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
  AND table_name <> ALL(%s)
  AND data_type IN ('character varying', 'text')
ORDER BY table_name, ordinal_position;
""" 

async def build_sample_map(conn, max_distinct: int = 30) -> dict:
    """
    For each text column: count distinct values. If few enough,
    grab 3 examples. Returns {(table, column): "'USA', 'Canada', 'France'"}
    """
    cur = await conn.execute(SAMPLE_TARGETS_SQL, (list(INTERNAL_TABLES),))
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

async def build_schema() -> dict:
    """Introspect once: {table: [(column, type_line, fk_target_table | None), ...]}."""
    async with get_ro_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(COLUMNS_SQL, (list(INTERNAL_TABLES),))
            columns = await cur.fetchall()

            await cur.execute(FK_SQL)
            fks = await cur.fetchall()

            samples = await build_sample_map(conn)

    # (table, column) -> (ref_table, ref_column)
    fk_map = {(table, col): (ref_table, ref_col) for table, col, ref_table, ref_col in fks}

    # Group columns by table, preserving ordinal order
    tables: dict[str, list[tuple]] = {}
    for table, col, raw_dtype in columns:
        dtype = TYPE_MAP.get(raw_dtype, raw_dtype)
        line = f"  {col} ({dtype})"
        ref = fk_map.get((table, col))
        if ref:
            line += f" -> {ref[0]}.{ref[1]}"
        ex = samples.get((table, col))
        if ex:
            line += f"  e.g. {ex}"
        tables.setdefault(table, []).append((col, line, ref[0] if ref else None))
    return tables


def render_schema(tables: dict, visible: frozenset[str] | None = None) -> str:
    """Prompt text for the given tables; foreign keys into hidden tables are left out."""
    blocks = []
    for table, cols in tables.items():
        if visible is not None and table not in visible:
            continue
        lines = []
        for _col, line, ref_table in cols:
            if ref_table and visible is not None and ref_table not in visible:
                line = line.split(" -> ")[0] + ("  e.g." + line.split("  e.g.", 1)[1] if "  e.g." in line else "")
            lines.append(line)
        blocks.append(f"Table: {table}\n" + "\n".join(lines))
    return "\n\n".join(blocks)


async def _schema() -> dict:
    """Cached introspection — built once, reused thereafter."""
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = await build_schema()
    return _schema_cache


async def get_schema_description(visible_tables: frozenset[str] | None = None) -> str:
    """Schema text for the model, limited to the tables a key may see."""
    return render_schema(await _schema(), visible_tables)


async def get_table_columns() -> dict[str, set[str]]:
    """{table: {column, ...}} for checking policies against the real schema."""
    return {table: {c[0] for c in cols} for table, cols in (await _schema()).items()}
