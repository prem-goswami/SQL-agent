from dataclasses import dataclass
import re
import sqlparse
from sqlparse import tokens as T

MAX_SQL_LENGTH = 10_000

DENIED_KEYWORDS = {"DROP", "DELETE", "TRUNCATE", "ALTER", "CREATE",
                   "INSERT", "UPDATE", "MERGE", "GRANT", "REVOKE"}
DENIED_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "set_config", "nextval", "setval",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_create", "lo_unlink", "lo_put", "lo_from_bytea",
    "dblink", "dblink_exec", "dblink_connect", "pg_terminate_backend",
    "pg_cancel_backend", "pg_reload_conf", "pg_notify", "pg_advisory_lock",
    "pg_advisory_xact_lock", "query_to_xml", "cursor_to_xml",
}


@dataclass
class ValidationResult:
    is_safe: bool
    reason: str | None = None
    cleaned_sql: str | None = None
    rule: str | None = None


FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(sql: str) -> str:
    return FENCE_RE.sub("", sql).strip()


def _sig(tokens):
    return [t for t in tokens if not t.is_whitespace and t.ttype not in T.Comment]


def _block(rule, reason):
    return ValidationResult(False, reason, rule=rule)


def validate_sql(sql: str) -> ValidationResult:
    try:
        return _validate(sql)
    except Exception as exc:  # never let the gate raise
        return _block("parse", f"Query could not be analysed safely: {type(exc).__name__}.")


def _validate(sql: str) -> ValidationResult:
    if not sql or not sql.strip():
        return _block("empty", "Empty SQL — the model returned no query.")
    if len(sql) > MAX_SQL_LENGTH:
        return _block("size", f"Query is longer than {MAX_SQL_LENGTH} characters.")
    cleaned = _strip_fences(sql)
    if not cleaned:
        return _block("empty", "Empty SQL after removing code fences.")
    statements = [s for s in sqlparse.parse(cleaned) if _sig(s.flatten())]
    if not statements:
        return _block("empty", "Could not parse any SQL statement.")
    if len(statements) > 1:
        return _block("single_statement",
                      f"Multiple statements detected ({len(statements)}). Only a single SELECT is allowed.")
    stmt = statements[0]
    stmt_type = stmt.get_type()
    if stmt_type != "SELECT":
        label = stmt_type if stmt_type != "UNKNOWN" else "unrecognised"
        return _block("statement_type", f"Statement type is {label}, not SELECT.")
    flat = _sig(stmt.flatten())
    for i, tok in enumerate(flat):
        word = tok.value.upper()
        if tok.ttype in T.Keyword and word == "INTO":
            return _block("select_into", "SELECT ... INTO creates a table; not allowed.")
        if tok.ttype in T.Keyword and word == "FOR" and i + 1 < len(flat) and \
                flat[i + 1].value.upper().split()[0] in {"UPDATE", "SHARE", "NO", "KEY"}:
            return _block("row_locking", "Row-locking clauses are not allowed.")
        if tok.ttype in (T.Keyword.DML, T.Keyword.DDL) and word in DENIED_KEYWORDS:
            return _block("data_modifying", f"Disallowed keyword '{word}' found in query.")
    for i, tok in enumerate(flat[:-1]):
        nxt = flat[i + 1]
        if nxt.ttype is T.Punctuation and nxt.value == "(":
            name = tok.value.strip('"').lower()
            if (tok.ttype in T.Name or tok.ttype is T.Literal.String.Symbol or tok.ttype in T.Keyword) \
                    and name in DENIED_FUNCTIONS:
                return _block("function", f"Function '{name}' is not allowed.")
    return ValidationResult(True, cleaned_sql=cleaned)
