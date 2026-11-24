import pytest
from app.validator import validate_sql

BAD = [
    ("", "empty"), ("SELECT 1; DROP TABLE customer", "single_statement"), ("DELETE FROM invoice", "statement_type"),
    ("SELECT * INTO stolen FROM customer", "select_into"), ("SELECT * FROM customer FOR UPDATE", "row_locking"),
    ("WITH d AS (DELETE FROM invoice RETURNING *) SELECT * FROM d", "data_modifying"),
    ("SELECT pg_sleep(30)", "function"), ("SELECT pg_catalog.pg_sleep(1)", "function"),
    ("SELECT set_config('default_transaction_read_only', 'off', false)", "function"),
    ("SELECT 1 FROM t WHERE x = '" + "a" * 20000 + "'", "size"),
]
GOOD = ["SELECT name FROM artist", "SELECT * FROM customer WHERE last_name = 'DROP TABLE'",
        "SELECT 'pg_sleep(5)' AS label", "```sql\nSELECT name FROM artist LIMIT 5\n```"]


@pytest.mark.parametrize("sql,rule", BAD)
def test_rejected_with_rule(sql, rule):
    v = validate_sql(sql)
    assert not v.is_safe and v.rule == rule


@pytest.mark.parametrize("sql", GOOD)
def test_accepted(sql):
    v = validate_sql(sql)
    assert v.is_safe and v.rule is None


def test_pathological_input_does_not_raise():
    v = validate_sql("SELECT " + "(" * 4000 + "1" + ")" * 4000)
    assert isinstance(v.is_safe, bool)
