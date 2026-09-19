"""
Final validation gate: checks generated SQL against the canonical
aml_data_model_schema.json (the full, authoritative schema) rather than only
the subset of tables the KG retrieval step happened to select for a given
question. This catches two things the KG-scoped repair in
text2sql_falkordb.py cannot:

  1. A real table/column that exists in the schema but fell outside the
     retrieval step's top_k selection (retrieval can be wrong; the full
     schema is the ground truth).
  2. Anything the KG-scoped repair missed or mis-corrected, as a second,
     independent check right before the SQL is handed to the user.

This module is deliberately independent of FalkorDB: it reads the schema
JSON file directly, so validation still works even if the knowledge graph is
unreachable.
"""

import difflib
import json
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "aml_data_model_schema.json")

SQL_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "join", "on", "as", "and", "or", "not",
    "in", "is", "null", "limit", "desc", "asc", "distinct", "count", "countif", "avg", "sum",
    "min", "max", "safe_divide", "date_trunc", "date", "timestamp_diff", "approx_quantiles",
    "offset", "month", "day", "year", "hour", "having", "union", "except", "intersect", "like",
    "between", "case", "when", "then", "else", "end", "over", "partition", "with", "all", "exists",
    "coalesce", "array_agg", "struct", "unnest", "cast", "extract",
}

_STRING_LITERAL_RE = re.compile(r"'[^']*'")
_TABLE_REF_RE = re.compile(r"\b(FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_ALIAS_DEF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_QUALIFIED_COL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")
_BARE_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def load_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_registry(schema=None):
    """Returns {table_name: set(field_names)} across both input and output
    data models, with output STRUCT subfields (e.g. Explainability's
    attributions.feature/.attribution) flattened in as valid column names
    too, since generated SQL may reference them unqualified after UNNEST."""
    schema = schema or load_schema()
    registry = {}

    for section in ("input_data_model", "output_data_model"):
        for table in schema[section]["tables"]:
            fields = set()
            for f in table["fields"]:
                fields.add(f["name"])
                for sub in f.get("subfields", []):
                    fields.add(sub["name"])
            registry[table["name"]] = fields

    return registry


_REGISTRY = None


def get_registry():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY


def _mask_literals(sql):
    literals = []

    def repl(m):
        literals.append(m.group(0))
        return f"__LIT{len(literals) - 1}__"

    return _STRING_LITERAL_RE.sub(repl, sql), literals


def _unmask_literals(sql, literals):
    for i, lit in enumerate(literals):
        sql = sql.replace(f"__LIT{i}__", lit)
    return sql


def validate_and_fix(sql, registry=None, table_cutoff=0.5, column_cutoff=0.6):
    """Validates + repairs `sql` against the canonical schema registry.

    Returns (fixed_sql, corrections, violations):
      - corrections: list of {"kind": "table"|"column", "from": ..., "to": ...}
        applied automatically via fuzzy matching.
      - violations: list of identifiers that looked like a table/column
        reference but couldn't be confidently matched to anything in the
        schema (left unchanged in the SQL; surfaced so the caller can flag
        low-confidence results instead of silently shipping a guess).
    """
    registry = registry or get_registry()
    all_tables = list(registry.keys())

    masked_sql, literals = _mask_literals(sql)
    corrections = []
    violations = []

    # --- 1. Table names -----------------------------------------------
    def fix_table(match):
        keyword, name = match.group(1), match.group(2)
        for t in all_tables:
            if t.lower() == name.lower():
                return f"{keyword} {t}"
        close = difflib.get_close_matches(name, all_tables, n=1, cutoff=table_cutoff)
        if close:
            corrections.append({"kind": "table", "from": name, "to": close[0]})
            return f"{keyword} {close[0]}"
        violations.append(f"table '{name}' not found in schema")
        return match.group(0)

    masked_sql = _TABLE_REF_RE.sub(fix_table, masked_sql)

    # --- 2. Alias -> table map (after table names are corrected) ------
    alias_to_table = {}
    for m in _ALIAS_DEF_RE.finditer(masked_sql):
        table_name, alias = m.group(1), m.group(2)
        resolved = next((t for t in all_tables if t.lower() == table_name.lower()), table_name)
        alias_to_table[alias.lower()] = resolved
    # Tables can also be referenced by their own name with no alias.
    tables_in_query = set(alias_to_table.values())
    for m in _TABLE_REF_RE.finditer(masked_sql):
        name = m.group(2)
        resolved = next((t for t in all_tables if t.lower() == name.lower()), None)
        if resolved:
            tables_in_query.add(resolved)
            alias_to_table.setdefault(resolved.lower(), resolved)

    fields_in_query = {f for t in tables_in_query for f in registry.get(t, set())}

    # --- 3. Qualified columns (alias.column / table.column) -----------
    def fix_qualified_column(match):
        prefix, col = match.group(1), match.group(2)
        table = alias_to_table.get(prefix.lower())
        candidate_fields = registry.get(table, set()) if table else fields_in_query
        for c in candidate_fields:
            if c.lower() == col.lower():
                return f"{prefix}.{c}"
        close = difflib.get_close_matches(col, list(candidate_fields), n=1, cutoff=column_cutoff)
        if close:
            corrections.append({"kind": "column", "from": col, "to": close[0]})
            return f"{prefix}.{close[0]}"
        # Fall back to searching every field in the whole schema, in case the
        # alias->table resolution itself was wrong.
        all_fields = {f for fs in registry.values() for f in fs}
        close = difflib.get_close_matches(col, list(all_fields), n=1, cutoff=column_cutoff)
        if close:
            corrections.append({"kind": "column", "from": col, "to": close[0]})
            return f"{prefix}.{close[0]}"
        violations.append(f"column '{prefix}.{col}' not found in schema")
        return match.group(0)

    masked_sql = re.sub(r"(\b[A-Za-z_][A-Za-z0-9_]*\.)\s+", r"\1", masked_sql)  # decoding-artifact fix
    masked_sql = _QUALIFIED_COL_RE.sub(fix_qualified_column, masked_sql)

    # --- 4. Bare (unqualified) column references -----------------------
    # Only touch identifiers that: aren't SQL keywords/functions, aren't a
    # known table name, aren't a short alias (T1, T2, ...), aren't already a
    # valid field of a table in the query, and aren't immediately preceded
    # by a dot (those were handled above). Aliases the query itself defines
    # via "AS <name>" are new names, not schema columns, so they're exempt.
    defined_aliases = {
        m.group(1).lower() for m in re.finditer(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", masked_sql, re.IGNORECASE)
    }

    def fix_bare_ident(match):
        word = match.group(0)
        start, end = match.start(), match.end()
        if start > 0 and masked_sql[start - 1] == ".":
            return word  # already handled as a qualified column
        lw = word.lower()
        if lw in SQL_KEYWORDS or lw in defined_aliases:
            return word
        if any(t.lower() == lw for t in all_tables):
            return word
        if re.fullmatch(r"t\d+", lw) or re.fullmatch(r"__lit\d+__", lw):
            return word
        if any(f.lower() == lw for f in fields_in_query):
            return word
        if word.isdigit():
            return word
        # Skip the alias immediately after "AS" (it's a new name, not a lookup).
        preceding = masked_sql[:start].rstrip()
        if preceding.lower().endswith(" as") or preceding.lower() == "as":
            return word
        close = difflib.get_close_matches(word, list(fields_in_query), n=1, cutoff=column_cutoff)
        if close and close[0].lower() != lw:
            corrections.append({"kind": "column", "from": word, "to": close[0]})
            return close[0]
        if len(word) >= 4:
            violations.append(f"identifier '{word}' does not match any table, alias, or column in scope")
        return word  # leave ambiguous/unresolvable bare words in place rather than guess

    masked_sql = _BARE_IDENT_RE.sub(fix_bare_ident, masked_sql)

    fixed_sql = _unmask_literals(masked_sql, literals)
    return fixed_sql, corrections, violations
