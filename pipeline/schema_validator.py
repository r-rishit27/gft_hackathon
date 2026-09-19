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
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "aml_data_model_schema.json")

SQL_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "join", "on", "as", "and", "or", "not",
    "in", "is", "null", "limit", "desc", "asc", "distinct", "count", "countif", "avg", "sum",
    "min", "max", "safe_divide", "date_trunc", "date", "timestamp_diff", "approx_quantiles",
    "offset", "month", "day", "year", "hour", "having", "union", "except", "intersect", "like",
    "between", "case", "when", "then", "else", "end", "over", "partition", "with", "all", "exists",
    "coalesce", "array_agg", "struct", "unnest", "cast", "extract",
}

# BigQuery treats both ' and " as string-literal delimiters (unlike ANSI SQL,
# where " denotes a quoted identifier) -- the model emits both styles, and
# missing either one here leaks literal values (e.g. "CARD") into the bare-
# identifier scan below, which then wrongly flags them as schema violations.
_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_TABLE_REF_RE = re.compile(r"\b(FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_ALIAS_DEF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_QUALIFIED_COL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")
_BARE_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_ENUM_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_LITERAL_COMPARISON_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*=\s*('[^']*'|\"[^\"]*\")"
)


def extract_enum_hint(description):
    """Pull literal enum values out of a field's schema description (e.g.
    "COMPANY or CONSUMER", "CARD, CASH, CHECK, WIRE, OTHER, or CRYPTO") --
    the canonical source both for the schema-serialization hints shown to the
    model (text2sql_falkordb.format_table) and for correcting literal-value
    casing here. Only ALL_CAPS_WITH_UNDERSCORE or short ALLCAPS words that
    appear in an enumerated list are picked up; schema-strict since the
    values come verbatim from the schema's own field.description text."""
    if not description:
        return []
    caps = _ENUM_TOKEN_RE.findall(description)
    plain = re.findall(r"\b[A-Z]{3,}\b", description)
    for word in plain:
        w = re.escape(word)
        # Catches every position in an enumerated list: "X, Y, or Z" --
        # "X," (leading), "or Z" (trailing), and "X or Y" (a bare two-item
        # list, where X has no comma after it but IS followed by "or").
        if word not in caps and re.search(rf"({w}\s*,|\bor\s+{w}\b|\b{w}\s+or\b)", description):
            caps.append(word)
    return sorted(set(caps))


def build_enum_registry(schema=None):
    """Returns {(table_name, field_name): [canonical enum values]} for every
    top-level field with known enum values. Prefers the schema's own explicit
    `allowed_enum_values` (authoritative, machine-generated from observed
    data) and falls back to regex-parsing the description for older schema
    snapshots that don't carry it.

    Deliberately top-level only: literal-casing correction only ever needs
    to resolve a bare column name actually referenced in generated SQL
    (e.g. `type = 'Company'`), and several nested struct fields share a leaf
    name with a different enum set within the same table (e.g. Party.type
    is COMPANY/CONSUMER, but Party.phone_numbers.type and
    Party.email_addresses.type are PERSONAL/CORPORATE) -- keying by
    (table_name, leaf_name) for those would silently overwrite one with the
    other. Nested fields aren't addressable as bare SQL identifiers anyway
    without an explicit UNNEST, so this is the correct scope, not a
    shortcut."""
    schema = schema or load_schema()
    enum_registry = {}
    for section in ("input_data_model", "output_data_model"):
        for table in schema[section]["tables"]:
            for f in table["fields"]:
                # allowed_enum_values can hold non-strings (e.g. BOOL fields
                # carry [false, true] as Python bools) -- only string enums
                # are relevant here since we're matching against quoted SQL
                # literals, and a bool would crash the later .lower() call.
                values = [v for v in (f.get("allowed_enum_values") or []) if isinstance(v, str)]
                if not values:
                    values = extract_enum_hint(f.get("description"))
                if values:
                    enum_registry[(table["name"], f["name"])] = list(values)
    return enum_registry


_ENUM_REGISTRY = None


def get_enum_registry():
    global _ENUM_REGISTRY
    if _ENUM_REGISTRY is None:
        _ENUM_REGISTRY = build_enum_registry()
    return _ENUM_REGISTRY


def build_field_descriptions(schema=None):
    """Returns {(table_name, field_name): description} across all nesting
    depths -- the gist text source for the semantic-repair fallback below.
    First occurrence per (table, field_name) wins; a RECORD field's own
    subfields never reuse its name within the same table's tree, so this
    can't silently overwrite a real value."""
    schema = schema or load_schema()
    desc = {}

    def walk(table_name, fields):
        for f in fields:
            key = (table_name, f["name"])
            if key not in desc:
                desc[key] = f.get("description", "") or ""
            nested = f.get("fields")
            if nested:
                walk(table_name, nested)

    for section in ("input_data_model", "output_data_model"):
        for table in schema[section]["tables"]:
            walk(table["name"], table["fields"])
    return desc


_FIELD_DESCRIPTIONS = None


def get_field_descriptions():
    global _FIELD_DESCRIPTIONS
    if _FIELD_DESCRIPTIONS is None:
        _FIELD_DESCRIPTIONS = build_field_descriptions()
    return _FIELD_DESCRIPTIONS


def build_table_descriptions(schema=None):
    """Returns {table_name: description}, the gist source for table-level
    semantic repair."""
    schema = schema or load_schema()
    return {
        table["name"]: table.get("description", "") or ""
        for section in ("input_data_model", "output_data_model")
        for table in schema[section]["tables"]
    }


_TABLE_DESCRIPTIONS = None


def get_table_descriptions():
    global _TABLE_DESCRIPTIONS
    if _TABLE_DESCRIPTIONS is None:
        _TABLE_DESCRIPTIONS = build_table_descriptions()
    return _TABLE_DESCRIPTIONS


_DESC_PREFIX_RE = re.compile(r"^(MANDATORY|RECOMMENDED|EXPERIMENTAL)\s*:\s*", re.IGNORECASE)


def _clean_description(description):
    """Strips the "MANDATORY:"/"RECOMMENDED:"/"EXPERIMENTAL:" classification
    prefix and keeps only the first sentence -- the same trim
    text2sql_falkordb.py's _clean_description_for_retrieval applies (kept as
    a small local duplicate rather than importing that module, to avoid a
    circular import: text2sql_falkordb imports this module, not the other
    way around). A full multi-sentence description measurably dilutes the
    embedding used for semantic repair below: "TYPICAL_TYPE" against
    RiskCaseEvent.type's full description scores 0.16 (fails any reasonable
    cutoff), but against just its first sentence scores 0.38."""
    if not description:
        return ""
    description = _DESC_PREFIX_RE.sub("", description)
    return description.split(". ")[0]


def _describe_field(name, preferred_tables, field_descs):
    """Best-effort description lookup for `name`: prefer a table already in
    play for this query (more likely to be the intended meaning when the
    same field name means slightly different things in different tables),
    falling back to any table that has a field with this name."""
    for t in preferred_tables:
        d = field_descs.get((t, name))
        if d is not None:
            return _clean_description(d)
    for (t, n), d in field_descs.items():
        if n == name:
            return _clean_description(d)
    return ""


# Calibrated the same way as pipeline/text2sql_falkordb.py's repair
# functions: a short hallucinated identifier embedded against a longer gist
# scores much lower in absolute cosine similarity than two comparable-length
# sentences do, so these sit well below the ~0.7+ range used for exemplar
# matching. See that module's TABLE_SEMANTIC_REPAIR_CUTOFF /
# COLUMN_SEMANTIC_REPAIR_CUTOFF docstring for the measured evidence.
TABLE_SEMANTIC_CUTOFF = 0.20
COLUMN_SEMANTIC_CUTOFF = 0.35


def _semantic_best_match(word, candidates_with_desc, cutoff):
    """candidates_with_desc: [(name, description), ...]. Returns the best-
    matching name if its embedding similarity to `word` clears `cutoff`,
    else None. Lazily imports semantic_search so this module still works
    standalone (per its own module docstring) if the embeddings dependency
    isn't installed -- repair just skips this step and falls through to
    flagging a violation, same as before this fallback existed."""
    if not candidates_with_desc:
        return None
    try:
        try:
            from pipeline import semantic_search
        except ImportError:
            import semantic_search
    except ImportError:
        return None

    word_vec = semantic_search.embed(word.replace("_", " "))
    gists = [f"{name}: {desc}" for name, desc in candidates_with_desc]
    gist_vecs = semantic_search.embed(gists)
    sims = [semantic_search.cosine_sim(word_vec, v) for v in gist_vecs]
    best_i = max(range(len(sims)), key=lambda i: sims[i])
    return candidates_with_desc[best_i][0] if sims[best_i] >= cutoff else None


def load_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _collect_field_names(fields, into):
    """Recursively flattens every field name -- at any nesting depth -- into
    `into`. RECORD-typed fields nest further fields under the key "fields"
    (arbitrary depth: e.g. Party.assets_value_range.start_amount.currency_code
    is three levels deep), so generated SQL may reference a deeply nested
    leaf name unqualified after UNNEST, same as the old flat "subfields"
    case this generalizes."""
    for f in fields:
        into.add(f["name"])
        nested = f.get("fields")
        if nested:
            _collect_field_names(nested, into)


def build_registry(schema=None):
    """Returns {table_name: set(field_names)} across both input and output
    data models, with every nested RECORD field flattened in as a valid
    column name too (see _collect_field_names)."""
    schema = schema or load_schema()
    registry = {}

    for section in ("input_data_model", "output_data_model"):
        for table in schema[section]["tables"]:
            fields = set()
            _collect_field_names(table["fields"], fields)
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


def _fuzzy_match_ci(word, candidates, cutoff):
    """Case-insensitive fuzzy match: difflib.SequenceMatcher penalizes a
    single case mismatch surprisingly heavily (e.g. "risk_Type" vs "type"
    scores 0.46, well under a 0.6 cutoff, while "risk_type" vs "type" scores
    0.62 and passes) -- comparing lowercased strings avoids fixing an
    identifier only when its casing happens to line up by chance. Returns
    the candidate in its original casing, or None."""
    candidates = list(candidates)
    lower_to_original = {c.lower(): c for c in candidates}
    close = difflib.get_close_matches(word.lower(), list(lower_to_original.keys()), n=1, cutoff=cutoff)
    return lower_to_original[close[0]] if close else None


def validate_and_fix(sql, registry=None, enum_registry=None, table_cutoff=0.5, column_cutoff=0.6):
    """Validates + repairs `sql` against the canonical schema registry.

    Returns (fixed_sql, corrections, violations):
      - corrections: list of {"kind": "table"|"column"|"literal_value", "from": ..., "to": ...}
        applied automatically via fuzzy/enum matching.
      - violations: list of identifiers that looked like a table/column
        reference but couldn't be confidently matched to anything in the
        schema (left unchanged in the SQL; surfaced so the caller can flag
        low-confidence results instead of silently shipping a guess).
    """
    registry = registry or get_registry()
    enum_registry = enum_registry or get_enum_registry()
    all_tables = list(registry.keys())

    masked_sql, literals = _mask_literals(sql)
    corrections = []
    violations = []

    # CTE names (WITH x AS (...), y AS (...)) are not schema tables -- they're
    # query-local, and their column list is whatever the CTE's own SELECT
    # defines (already covered by the "AS <alias>" defined_aliases scan
    # below), so they're exempt from both table and bare-identifier checks.
    cte_names = {m.group(1).lower() for m in re.finditer(r"\bWITH\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", masked_sql, re.IGNORECASE)}
    cte_names |= {m.group(1).lower() for m in re.finditer(r"\)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", masked_sql, re.IGNORECASE)}

    table_desc = get_table_descriptions()
    field_descs = get_field_descriptions()

    # --- 1. Table names -----------------------------------------------
    def fix_table(match):
        keyword, name = match.group(1), match.group(2)
        if name.lower() in cte_names:
            return match.group(0)
        for t in all_tables:
            if t.lower() == name.lower():
                return f"{keyword} {t}"
        close = _fuzzy_match_ci(name, all_tables, table_cutoff)
        if close:
            corrections.append({"kind": "table", "from": name, "to": close})
            return f"{keyword} {close}"
        sem = _semantic_best_match(
            name, [(t, _clean_description(table_desc.get(t, ""))) for t in all_tables], TABLE_SEMANTIC_CUTOFF
        )
        if sem:
            corrections.append({"kind": "table", "stage": "semantic", "from": name, "to": sem})
            return f"{keyword} {sem}"
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
        close = _fuzzy_match_ci(col, candidate_fields, column_cutoff)
        if close:
            corrections.append({"kind": "column", "from": col, "to": close})
            return f"{prefix}.{close}"
        # Fall back to searching every field in the whole schema, in case the
        # alias->table resolution itself was wrong.
        all_fields = {f for fs in registry.values() for f in fs}
        close = _fuzzy_match_ci(col, all_fields, column_cutoff)
        if close:
            corrections.append({"kind": "column", "from": col, "to": close})
            return f"{prefix}.{close}"
        pool = candidate_fields or all_fields
        preferred = [table] if table else list(tables_in_query)
        sem = _semantic_best_match(
            col, [(c, _describe_field(c, preferred, field_descs)) for c in pool], COLUMN_SEMANTIC_CUTOFF
        )
        if sem:
            corrections.append({"kind": "column", "stage": "semantic", "from": col, "to": sem})
            return f"{prefix}.{sem}"
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
        if lw in SQL_KEYWORDS or lw in defined_aliases or lw in cte_names:
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
        close = _fuzzy_match_ci(word, fields_in_query, column_cutoff)
        if close and close.lower() != lw:
            corrections.append({"kind": "column", "from": word, "to": close})
            return close
        if len(word) >= 4 and fields_in_query:
            sem = _semantic_best_match(
                word,
                [(c, _describe_field(c, list(tables_in_query), field_descs)) for c in fields_in_query],
                COLUMN_SEMANTIC_CUTOFF,
            )
            if sem:
                corrections.append({"kind": "column", "stage": "semantic", "from": word, "to": sem})
                return sem
        if len(word) >= 4:
            violations.append(f"identifier '{word}' does not match any table, alias, or column in scope")
        return word  # leave ambiguous/unresolvable bare words in place rather than guess

    masked_sql = _BARE_IDENT_RE.sub(fix_bare_ident, masked_sql)

    fixed_sql = _unmask_literals(masked_sql, literals)

    # --- 5. Literal enum-value casing (e.g. "Company" -> "COMPANY") ---
    # Only corrects casing/exact-token drift when the literal case-
    # insensitively matches one of the field's known enum values (parsed
    # from its schema description) -- never invents a value that isn't a
    # documented enum member, and skips ambiguous bare-column references
    # that match a same-named field in more than one table in the query.
    def fix_literal(match):
        col_ref, literal = match.group(1), match.group(2)
        quote = literal[0]
        value = literal[1:-1]

        if "." in col_ref:
            alias, field = col_ref.split(".", 1)
            table = alias_to_table.get(alias.lower())
        else:
            field = col_ref
            candidates = [t for t in tables_in_query if any(f.lower() == field.lower() for f in registry.get(t, set()))]
            table = candidates[0] if len(candidates) == 1 else None

        if not table:
            return match.group(0)

        canonical_field = next((f for f in registry.get(table, set()) if f.lower() == field.lower()), None)
        if canonical_field is None:
            return match.group(0)

        enum_values = enum_registry.get((table, canonical_field))
        if not enum_values:
            return match.group(0)

        canonical_value = next((v for v in enum_values if v.lower() == value.lower()), None)
        if canonical_value and canonical_value != value:
            corrections.append({"kind": "literal_value", "from": value, "to": canonical_value})
            return f"{col_ref} = {quote}{canonical_value}{quote}"
        return match.group(0)

    fixed_sql = _LITERAL_COMPARISON_RE.sub(fix_literal, fixed_sql)

    return fixed_sql, corrections, violations
