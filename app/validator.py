from dataclasses import dataclass
import re
import sqlparse

DENIED_KEYWORDS = {
    "DROP", "DELETE", "TRUNCATE", "ALTER", "CREATE",
    "INSERT", "UPDATE", "GRANT", "REVOKE", "COMMENT",
    "COPY", "MERGE", "CALL", "DO", "VACUUM", "REINDEX",
}

KEYWORD_TYPES = (
    sqlparse.tokens.Keyword,
    sqlparse.tokens.Keyword.DDL,
    sqlparse.tokens.Keyword.DML,
)



@dataclass
class ValidationResult:
    is_safe: bool
    reason: str | None = None
    cleaned_sql: str | None = None


FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(sql: str) -> str:
    """Remove markdown code fences the LLM adds despite instructions."""
    return FENCE_RE.sub("", sql).strip()


def validate_sql(sql: str) -> ValidationResult:
    if not sql or not sql.strip():
        return ValidationResult(False, "Empty SQL — the model returned no query.")

    cleaned = _strip_fences(sql)
    if not cleaned:
        return ValidationResult(False, "Empty SQL after removing code fences.")

    statements = sqlparse.parse(cleaned)

    # Rule 1 — structure: exactly one statement.
    if len(statements) == 0:
        return ValidationResult(False, "Could not parse any SQL statement.")
    if len(statements) > 1:
        return ValidationResult(
            False,
            f"Multiple statements detected ({len(statements)}). "
            "Only a single SELECT is allowed.",
        )

    stmt = statements[0]

    # Rule 2 — type: it must be a SELECT.
    stmt_type = stmt.get_type()
    if stmt_type != "SELECT":
        label = stmt_type if stmt_type != "UNKNOWN" else "unrecognised"
        return ValidationResult(
            False, f"Statement type is {label}, not SELECT."
        )

    # Rule 3 — keywords: redundant denylist over parsed tokens.
    for token in stmt.flatten():
        if token.ttype in KEYWORD_TYPES:
            word = token.value.upper()
            if word in DENIED_KEYWORDS:
                return ValidationResult(
                    False, f"Disallowed keyword '{word}' found in query."
                )

    return ValidationResult(True, cleaned_sql=cleaned)