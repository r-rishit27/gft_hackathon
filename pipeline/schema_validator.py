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
_LITERAL_COMPARISON_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*=\s*('[^']*'|\"[^\"]*\")"
)


def build_enum_registry(schema=None):
    """Returns {(table_name, field_name): [canonical enum values]} for every
    top-level field with known enum values, from the schema's own explicit
    `allowed_enum_values` (authoritative, machine-generated from observed
    data -- always present for this schema, so no regex-guessing fallback;
    see the comment inline below for why that was actively wrong here).

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
                # No regex fallback onto the description: allowed_enum_values
                # is always authoritative for this schema (present as a list
                # or explicitly null -- "confirmed no enum", not "unknown"),
                # and several fields with genuinely no enum mention a
                # *different* field's enum in cross-referencing prose (e.g.
                # Party.birth_date's description says "...where Party.type =
                # CONSUMER..."), which the regex extractor picked up as if it
                # were the field's own enum.
                values = [v for v in (f.get("allowed_enum_values") or []) if isinstance(v, str)]
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
    """Returns {table_name: description} -- the bare table description only.
    Kept for callers that just want the description text; table-level
    semantic repair itself uses build_table_gists() below, not this."""
    schema = schema or load_schema()
    return {
        table["name"]: table.get("description", "") or ""
        for section in ("input_data_model", "output_data_model")
        for table in schema[section]["tables"]
    }


def build_table_gists(schema=None):
    """Returns {table_name: gist} where the gist mirrors
    pipeline/text2sql_falkordb.py's _table_gist (description + field names +
    enum values). Calibrating table-level semantic repair against real
    observed hallucinations showed the bare table description alone isn't
    enough to separate genuine matches from noise: "SAR" against
    RiskCaseEvent's *description* alone scores only 0.21 -- barely above a
    clearly-wrong negative ("Model" scored 0.33) -- because the description
    never mentions SAR; only the type field's enum value AML_SAR does. The
    retrieval step already needed this same enrichment for the same reason;
    repair needs it too, or it can't tell a real match from noise."""
    schema = schema or load_schema()
    gists = {}
    for section in ("input_data_model", "output_data_model"):
        for table in schema[section]["tables"]:
            field_parts = []
            for f in table["fields"]:
                # No regex fallback here either -- see build_enum_registry's
                # comment for why that's wrong for this schema.
                vals = [v for v in (f.get("allowed_enum_values") or []) if isinstance(v, str)]
                field_parts.append(f'{f["name"]} ({" ".join(vals)})' if vals else f["name"])
            desc = _clean_description(table.get("description", ""))
            gists[table["name"]] = f'{desc}. Fields: {" ".join(field_parts)}'
    return gists


_TABLE_GISTS = None


def get_table_gists():
    global _TABLE_GISTS
    if _TABLE_GISTS is None:
        _TABLE_GISTS = build_table_gists()
    return _TABLE_GISTS


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


# Calibrated against a labeled set of real hallucinations observed across
# this session's eval runs (positive: should repair; negative: garbage that
# should stay an honest violation) -- not guessed or hand-picked. A short
# hallucinated identifier embedded against a longer gist scores much lower
# in absolute cosine similarity than two comparable-length sentences do, so
# both sit well below the ~0.7+ range used for exemplar matching.
#
# COLUMN_SEMANTIC_CUTOFF=0.35: clean separation exists. Garbage tokens
# ("SELECTION", "WHERES") scored 0.11-0.23; genuine repairs ("TYPICAL_TYPE"
# -> type, "risk_level" -> risk_score) scored 0.38-0.57.
#
# TABLE_SEMANTIC_CUTOFF=0.30: no cutoff cleanly separates every case tested.
# "SAR"/"sar_filing" (should match RiskCaseEvent) scored only 0.23-0.25,
# *below* a clear negative ("Model", a real hallucination with no matching
# table) at 0.28. Rather than pick a lower cutoff that lets "Model"-class
# negatives through as false corrections, this sits above the negative and
# deliberately gives up the SAR-class repairs -- consistent with this
# module's overall stance of leaving an unresolvable identifier flagged as a
# violation rather than risking a wrong "fix". Clearer cases (transactionlog
# -> Transaction 0.50, "login interaction event" -> InteractionEvent 0.48)
# still pass with a comfortable margin.
TABLE_SEMANTIC_CUTOFF = 0.30
COLUMN_SEMANTIC_CUTOFF = 0.35

# LITERAL_SEMANTIC_CUTOFF=0.5: comparing two short, comparable-length strings
# (a value against an enum member) doesn't suffer the length-asymmetry
# problem table/column matching does, so separation is clean and wide.
# Genuine matches ("Primary"->PRIMARY_HOLDER, "login"->LOGIN) scored 0.70+;
# unrelated values ("Yes", "active") scored under 0.28 against every member
# of an unrelated enum.
LITERAL_SEMANTIC_CUTOFF = 0.5


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
    the candidate in its original casing, or None.

    Short words get a much tighter effective cutoff: character-overlap
    similarity is noisy at short lengths -- "sar" vs "party" scores exactly
    0.5 from two coincidentally shared letters, which would silently
    "fix" a hallucinated "SAR" table into "Party" at the default table
    cutoff. Below 5 characters, only accept a very close match (>=0.75);
    anything less confident should fall through to the semantic fallback
    (or an honest violation) instead of a coin-flip character match."""
    candidates = list(candidates)
    if not candidates:
        return None
    lower_to_original = {c.lower(): c for c in candidates}
    effective_cutoff = max(cutoff, 0.75) if len(word) < 5 else cutoff
    close = difflib.get_close_matches(word.lower(), list(lower_to_original.keys()), n=1, cutoff=effective_cutoff)
    return lower_to_original[close[0]] if close else None


def check_sql_syntax(sql):
    """Coarse structural sanity checks -- not a real SQL parser, just the
    cheap, unambiguous checks that catch gross malformation no amount of
    schema grounding can see (e.g. "SELECT count(*) FROM CommercialPartiesRegistration
    WHERE party_id NOT IN ( SELECTION DISTINCT party_id FROM Party" -- missing
    a closing paren). Every one of these checks only fires on something that
    is *always* wrong in valid SQL, so it can't produce a false positive on
    a legitimately unusual but correct query."""
    issues = []

    depth = 0
    in_single, in_double = False, False
    i = 0
    while i < len(sql):
        c = sql[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "(" and not in_single and not in_double:
            depth += 1
        elif c == ")" and not in_single and not in_double:
            depth -= 1
            if depth < 0:
                issues.append("unmatched closing parenthesis")
                depth = 0  # keep scanning for a legitimate count elsewhere
        i += 1
    if depth > 0:
        issues.append(f"{depth} unclosed parenthesis/parentheses")
    if in_single:
        issues.append("unterminated single-quoted string literal")
    if in_double:
        issues.append("unterminated double-quoted string literal")

    if not re.search(r"\bSELECT\b", sql, re.IGNORECASE):
        issues.append("missing SELECT clause")
    if not re.search(r"\bFROM\b", sql, re.IGNORECASE):
        issues.append("missing FROM clause")
    if re.search(r",\s*,", sql):
        issues.append("consecutive commas (likely a missing list item)")
    if re.search(r",\s*(FROM|WHERE|GROUP BY|ORDER BY|HAVING)\b", sql, re.IGNORECASE):
        issues.append("trailing comma before a clause keyword")

    return issues


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

    table_gists = get_table_gists()
    field_descs = get_field_descriptions()
    table_candidates = [(t, table_gists.get(t, "")) for t in all_tables]

    # --- 1. Table names -----------------------------------------------
    # A hallucinated table name is sometimes more than one bare word (e.g.
    # "FROM login interaction event" -- the model meant InteractionEvent but
    # spelled it as three loose words instead of one identifier). A plain
    # regex substitution can only ever replace the single word captured by
    # _TABLE_REF_RE, so this is a manual scan: after the first word fails
    # exact/fuzzy/semantic matching alone, greedily grab up to 2 more
    # trailing bare words (stopping at any SQL keyword) and retry the
    # semantic match against the whole phrase -- if that resolves
    # confidently, the extra words are consumed (removed) along with the
    # first, since they were never a second FROM source or extra clause.
    _TRAILING_WORD_RE = re.compile(r"\s+([A-Za-z_][A-Za-z0-9_]*)")

    def _consume_extra_words(text, start, max_extra=2):
        words, spans, idx = [], [], start
        while len(words) < max_extra:
            m = _TRAILING_WORD_RE.match(text, idx)
            if not m or m.group(1).lower() in SQL_KEYWORDS:
                break
            words.append(m.group(1))
            spans.append(m.end())
            idx = m.end()
        return words, spans

    out_parts = []
    pos = 0
    for m in _TABLE_REF_RE.finditer(masked_sql):
        if m.start() < pos:
            continue  # already consumed as part of a previous multi-word phrase
        out_parts.append(masked_sql[pos:m.start()])
        keyword, name = m.group(1), m.group(2)

        if name.lower() in cte_names:
            out_parts.append(m.group(0))
            pos = m.end()
            continue

        exact = next((t for t in all_tables if t.lower() == name.lower()), None)
        if exact:
            out_parts.append(f"{keyword} {exact}")
            pos = m.end()
            continue

        close = _fuzzy_match_ci(name, all_tables, table_cutoff)
        if close:
            corrections.append({"kind": "table", "from": name, "to": close})
            out_parts.append(f"{keyword} {close}")
            pos = m.end()
            continue

        extra_words, extra_spans = _consume_extra_words(masked_sql, m.end())
        if extra_words:
            phrase = " ".join([name] + extra_words)
            sem_phrase = _semantic_best_match(phrase, table_candidates, TABLE_SEMANTIC_CUTOFF)
            if sem_phrase:
                corrections.append({
                    "kind": "table", "stage": "semantic_phrase", "from": phrase, "to": sem_phrase,
                })
                out_parts.append(f"{keyword} {sem_phrase}")
                pos = extra_spans[-1]
                continue

        sem = _semantic_best_match(name, table_candidates, TABLE_SEMANTIC_CUTOFF)
        if sem:
            corrections.append({"kind": "table", "stage": "semantic", "from": name, "to": sem})
            out_parts.append(f"{keyword} {sem}")
            pos = m.end()
            continue

        violations.append(f"table '{name}' not found in schema")
        out_parts.append(m.group(0))
        pos = m.end()

    out_parts.append(masked_sql[pos:])
    masked_sql = "".join(out_parts)

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
        # Length floor of 3 (not the previous 4): short real identifiers
        # already exit above via the alias (t\d+) and known-table/-field
        # checks, and SAR-length garbled fragments (e.g. a stray 3-letter
        # token) deserve the same repair attempt and, failing that, the same
        # violation flag as longer ones -- silently ignoring them just hides
        # the failure instead of catching it.
        if len(word) >= 3 and fields_in_query:
            sem = _semantic_best_match(
                word,
                [(c, _describe_field(c, list(tables_in_query), field_descs)) for c in fields_in_query],
                COLUMN_SEMANTIC_CUTOFF,
            )
            if sem:
                corrections.append({"kind": "column", "stage": "semantic", "from": word, "to": sem})
                return sem
        if len(word) >= 3:
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
        if canonical_value:
            if canonical_value != value:
                corrections.append({"kind": "literal_value", "from": value, "to": canonical_value})
                return f"{col_ref} = {quote}{canonical_value}{quote}"
            return match.group(0)  # already exactly correct

        # The value doesn't case-insensitively match any real enum member at
        # all -- previously this was silently accepted as "just a string",
        # even for a field with a fixed, known set of valid values (e.g.
        # `role = 'Primary'` when the only real values are PRIMARY_HOLDER/
        # SECONDARY_HOLDER/SUPPLEMENTARY_HOLDER). Try a semantic match
        # against the enum's own members first (calibrated: genuine matches
        # like "Primary"->PRIMARY_HOLDER score 0.70+, unrelated values like
        # "Yes" or "active" score under 0.28 against any real member, so 0.5
        # separates them with a comfortable margin); if nothing clears the
        # bar, flag it rather than ship a value that will never match a real
        # row.
        sem = _semantic_best_match(value, [(v, v) for v in enum_values], LITERAL_SEMANTIC_CUTOFF)
        if sem:
            corrections.append({"kind": "literal_value", "stage": "semantic", "from": value, "to": sem})
            return f"{col_ref} = {quote}{sem}{quote}"
        violations.append(
            f"literal {quote}{value}{quote} is not a valid value for {table}.{canonical_field}; "
            f"expected one of: {', '.join(enum_values)}"
        )
        return match.group(0)

    fixed_sql = _LITERAL_COMPARISON_RE.sub(fix_literal, fixed_sql)

    # Final structural sanity pass -- catches gross malformation (unbalanced
    # parens/quotes, missing SELECT/FROM, stray commas) that identifier-level
    # repair has no way to see, since none of it is schema-grounding.
    violations.extend(f"syntax: {issue}" for issue in check_sql_syntax(fixed_sql))

    return fixed_sql, corrections, violations
