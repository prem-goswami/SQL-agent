"""Access policies attached to API keys.

A policy is a JSON document:

    {
      "name": "analyst_brazil_canada",
      "tables": ["customer", "invoice", "invoice_line", "track"],
      "columns": {"customer": {"deny": ["email", "phone"]}},
      "rows": {"customer": {"country": ["Brazil", "Canada"]}}
    }

- tables:  the tables the key may use. Omitted means every table.
- columns: per table, columns the key may not read.
- rows:    per table, column -> allowed values; a row is visible when every
           listed column holds one of its values.

Currently only "tables" is applied, and only to the schema description the
model is given. "columns" and "rows" are validated and stored with the key
but not enforced yet.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_KEYS = {"name", "tables", "columns", "rows"}


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    name: str
    tables: frozenset[str] | None = None
    columns: dict[str, frozenset[str]] = field(default_factory=dict)
    rows: dict[str, dict[str, tuple]] = field(default_factory=dict)

    def allows_table(self, table: str) -> bool:
        return self.tables is None or table in self.tables

    def to_json(self) -> dict:
        out: dict = {"name": self.name}
        if self.tables is not None:
            out["tables"] = sorted(self.tables)
        if self.columns:
            out["columns"] = {t: {"deny": sorted(c)} for t, c in self.columns.items()}
        if self.rows:
            out["rows"] = {t: {c: list(v) for c, v in cols.items()} for t, cols in self.rows.items()}
        return out


def _str_list(value, where: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise PolicyError(f"{where} must be a non-empty list of names")
    return value


def parse_policy(doc: dict) -> Policy:
    """Validate the structure of a policy document and build a Policy."""
    if not isinstance(doc, dict):
        raise PolicyError("a policy must be a JSON object")
    unknown = set(doc) - ALLOWED_KEYS
    if unknown:
        raise PolicyError(f"unknown policy keys: {', '.join(sorted(unknown))}")
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PolicyError("policy needs a non-empty name")

    tables = None
    if "tables" in doc:
        tables = frozenset(_str_list(doc["tables"], "tables"))

    columns: dict[str, frozenset[str]] = {}
    for table, spec in (doc.get("columns") or {}).items():
        if not isinstance(spec, dict) or set(spec) != {"deny"}:
            raise PolicyError(f'columns.{table} must be {{"deny": [...]}}')
        columns[table] = frozenset(_str_list(spec["deny"], f"columns.{table}.deny"))

    rows: dict[str, dict[str, tuple]] = {}
    for table, spec in (doc.get("rows") or {}).items():
        if not isinstance(spec, dict) or not spec:
            raise PolicyError(f"rows.{table} must map column names to allowed values")
        rows[table] = {}
        for col, values in spec.items():
            if not isinstance(values, list) or not values or not all(
                isinstance(v, (str, int)) and not isinstance(v, bool) for v in values
            ):
                raise PolicyError(f"rows.{table}.{col} must be a non-empty list of strings or integers")
            rows[table][col] = tuple(values)

    for section in (columns, rows):
        for table in section:
            if tables is not None and table not in tables:
                raise PolicyError(f"{table} has column or row rules but is not in tables")
    return Policy(name=name.strip(), tables=tables, columns=columns, rows=rows)


def load_policy_file(path: str | Path) -> Policy:
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    return parse_policy(doc)


def check_against_schema(policy: Policy, schema: dict[str, set[str]]) -> None:
    """Reject policies that name tables or columns the database does not have."""
    named = set(policy.tables or ()) | set(policy.columns) | set(policy.rows)
    missing = sorted(t for t in named if t not in schema)
    if missing:
        raise PolicyError(f"unknown tables: {', '.join(missing)}")
    for table, cols in list(policy.columns.items()) + [(t, set(c)) for t, c in policy.rows.items()]:
        bad = sorted(c for c in cols if c not in schema[table])
        if bad:
            raise PolicyError(f"unknown columns in {table}: {', '.join(bad)}")
