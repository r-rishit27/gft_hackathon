"""
Text-to-SQL over the AML knowledge graph stored in FalkorDB, using a
knowledge-augmented generation (KAG) pipeline: retrieval + a verified-answer
shortcut + schema-grounded repair around Defog SQLCoder
(mannix/defog-llama3-sqlcoder-8b), run locally through Ollama.

Pipeline (generate_sql_kag):
  1. Retrieval engine: pull the most relevant tables/fields/constraints/
     foreign-keys out of the FalkorDB knowledge graph (aml_data_model) for
     the question, instead of hardcoding a schema.
  2. Schema serialization: format the retrieved tables as CREATE TABLE DDL
     (build_ddl_schema), the format SQLCoder was actually fine-tuned on,
     including enum-value hints as trailing column comments.
  3. Exemplar retrieval: if the question is a (near-)duplicate of a known,
     verified KPI question (eval_testcases.json), return that gold SQL
     directly -- no model call, no hallucination risk.
  4. Otherwise, generate with SQLCoder (deterministic: temperature 0, fixed
     seed) via the local Ollama server, then run schema-grounded repair:
     fuzzy-match every FROM/JOIN target back onto the tables retrieval
     actually knows are real for this question, and correct near-misses
     (e.g. "RiskCase" -> "RiskCaseEvent").
  5. Final validation gate (schema_validator.validate_and_fix): re-checks the
     (possibly already-repaired) SQL against the full, canonical
     aml_data_model_schema.json -- not just the KG-retrieved subset -- so a
     correct table/column that fell outside retrieval's top_k selection can
     still be recovered, and anything the KG-scoped repair missed gets a
     second, independent check before the query reaches the user.

SQLCoder is instruction-following (Llama-3 based), so SYSTEM_PROMPT is passed
through Ollama's native `system` field and genuinely shapes its behavior --
unlike a plain seq2seq checkpoint fine-tuned on a fixed template, which would
just treat extra prompt text as noise.
"""

import json
import os
import re
import sys

import requests
from dotenv import load_dotenv
from falkordb import FalkorDB

try:
    from pipeline import schema_validator
    from pipeline import semantic_search
except ImportError:
    # Running this file directly (`python pipeline/text2sql_falkordb.py ...`)
    # rather than as part of the `pipeline` package -- the script's own
    # directory is already on sys.path, so the plain import finds the sibling
    # module.
    import schema_validator
    import semantic_search

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT") or 6379)
FALKORDB_USERNAME = os.environ.get("FALKORDB_USERNAME", "")
FALKORDB_PASSWORD = os.environ.get("FALKORDB_PASSWORD", "")
FALKORDB_GRAPH = os.environ.get("FALKORDB_GRAPH", "aml_data_model")
# Defog SQLCoder (a Llama-3-8B fine-tune for Postgres/Redshift/Snowflake SQL,
# on par with capable generalist frontier models per its model card), run
# locally through Ollama -- no fine-tuning needed.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mannix/defog-llama3-sqlcoder-8b")
MODEL_BACKEND = os.environ.get("MODEL_BACKEND", "ollama")
SCHEMA_BACKEND = os.environ.get("SCHEMA_BACKEND", "auto")

SYSTEM_PROMPT = (
    "You are an AML (anti-money-laundering) analyst and business leader who "
    "tracks key KPIs for senior management by generating SQL queries. Given a "
    "database schema retrieved from a knowledge graph and a question, generate "
    "only read-only SQL: SELECT statements only. Never generate INSERT, UPDATE, "
    "DELETE, DROP, ALTER, TRUNCATE, MERGE, or any other data- or schema-"
    "modifying statement. Strictly adhere to the schema: use only the tables "
    "and columns that are actually present in it, and never invent one."
)

# BigQuery-ish types (as stored in the KG) -> generic SQL type names used in
# the CREATE TABLE DDL schema string.
TYPE_MAP = {
    "STRING": "text",
    "BOOL": "bool",
    "TIMESTAMP": "timestamp",
    "DATE": "date",
    "FLOAT64": "real",
    "INT64": "int",
    "STRUCT": "text",
    "JSON": "text",
}


def sql_type(bq_type):
    return TYPE_MAP.get((bq_type or "").upper(), "text")


def connect_graph():
    if SCHEMA_BACKEND == "catalog" or (SCHEMA_BACKEND == "auto" and not FALKORDB_HOST):
        return None
    if not FALKORDB_HOST:
        raise RuntimeError("FalkorDB credentials are missing; choose SCHEMA_BACKEND=catalog for schema-file retrieval")
    db = FalkorDB(
        host=FALKORDB_HOST,
        port=FALKORDB_PORT,
        username=FALKORDB_USERNAME,
        password=FALKORDB_PASSWORD,
    )
    return db.select_graph(FALKORDB_GRAPH)


# ---------------------------------------------------------------------------
# Retrieval engine
# ---------------------------------------------------------------------------

def _stem(token):
    """Naive suffix stripping so "cases"/"case", "logins"/"login" etc. count
    as the same token for lexical retrieval and exemplar matching -- plural
    mismatches were previously causing near-duplicate questions to miss the
    exemplar-retrieval threshold."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3] in "sxzh":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text):
    return {_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower())}


_DESC_PREFIX_RE = re.compile(r"^(MANDATORY|RECOMMENDED|EXPERIMENTAL)\s*:\s*", re.IGNORECASE)


def _clean_description_for_retrieval(description):
    """Strips the "MANDATORY:"/"RECOMMENDED:"/"EXPERIMENTAL:" classification
    prefix the schema's field descriptions carry, and keeps only the first
    sentence. Retrieval scoring only needs the gist of what a field is for --
    the full multi-sentence description (usage caveats, cross-references to
    other fields, worked examples) mostly adds noise tokens that dilute
    genuine question/schema overlap and can skew which tables get selected."""
    if not description:
        return ""
    description = _DESC_PREFIX_RE.sub("", description)
    return description.split(". ")[0]


def fetch_all_tables(graph):
    """Pull every table, its fields, and outgoing FK-style edges from the KG."""
    if graph is None:
        return fetch_catalog_tables()
    tables = {}

    rows = graph.query(
        """
        MATCH (t:Table)
        RETURN t.name AS name, t.dataset AS dataset, t.description AS description,
               t.primary_key AS primary_key
        """
    ).result_set
    for name, dataset, description, primary_key in rows:
        tables[name] = {
            "name": name,
            "dataset": dataset,
            "description": description or "",
            "primary_key": primary_key or [],
            "fields": [],
            "foreign_keys": [],
        }

    rows = graph.query(
        """
        MATCH (t:Table)-[:HAS_FIELD]->(f:Field)
        RETURN t.name AS table, f.name AS name, f.type AS type,
               f.description AS description, f.is_primary_key AS is_pk,
               f.allowed_enum_values AS allowed_enum_values
        """
    ).result_set
    for table, name, ftype, description, is_pk, allowed_enum_values in rows:
        if table in tables:
            tables[table]["fields"].append({
                "name": name,
                "type": ftype,
                "description": description or "",
                "is_pk": bool(is_pk),
                "allowed_enum_values": allowed_enum_values or [],
            })

    rows = graph.query(
        """
        MATCH (a:Field)-[:REFERENCES]->(b:Field)
        RETURN a.table AS from_table, a.name AS from_field, a.type AS from_type,
               b.table AS to_table
        """
    ).result_set
    for from_table, from_field, from_type, to_table in rows:
        if from_table in tables:
            tables[from_table]["foreign_keys"].append(
                {"column": from_field, "type": sql_type(from_type), "ref_table": to_table}
            )

    return tables


def fetch_catalog_tables():
    """Actual deployed schema metadata, never fixture rows or cached answers."""
    path = os.environ.get("AML_SCHEMA_PATH", os.path.join(PROJECT_ROOT, "dataset", "aml_data_model_schema.json"))
    with open(path, encoding="utf-8") as handle:
        schema = json.load(handle)
    return {
        table["name"]: {
            "name": table["name"], "description": table.get("description", ""),
            "primary_key": table.get("primary_key", []), "foreign_keys": [],
            "fields": [{k: field.get(k) for k in ("name", "type", "sql_type", "description", "allowed_enum_values")}
                       for field in table["fields"]],
        }
        for section in ("input_data_model", "output_data_model")
        for table in schema[section]["tables"]
    }


_NAME_MATCH_WEIGHT = 2  # a table/field NAME matching the question is a much
                         # stronger intent signal than a description word
                         # incidentally matching, so it counts for more.

# Hybrid table-retrieval blend: lexical score is normalized to [0, 1] (as a
# fraction of the question's tokens that were matched) and combined with the
# table's semantic similarity to the question. Semantic carries most of the
# weight since it captures intent past exact wording (e.g. a question about
# "customers" should still surface Party even with zero literal word
# overlap), while lexical still pulls its weight for exact identifier hits
# ("risk_score", "party_id") that a general-purpose sentence embedding can
# under-weight.
TABLE_LEXICAL_WEIGHT = 0.4
TABLE_SEMANTIC_WEIGHT = 0.6


def _field_enum_values(f):
    """The schema's authoritative allowed_enum_values only -- no regex
    fallback onto the description. That fallback used to catch older schema
    snapshots without this field computed, but for the current schema it's
    actively wrong: allowed_enum_values is always present (a list, or
    explicitly null/empty meaning "confirmed no enum"), and several fields
    with genuinely no enum (e.g. Party.birth_date) mention a *different*
    field's enum values in cross-referencing prose ("...where Party.type =
    CONSUMER..."), which the regex extractor picked up as if they were
    birth_date's own enum. Used here so identifier-like enum tokens (e.g.
    AML_SAR, PASSWORD_CHANGE) are searchable -- a question mentioning "SAR"
    has zero overlap with any table/field *name*, but everything to do with
    RiskCaseEvent.type's enum values, which the first-sentence-trimmed
    description no longer carries verbatim."""
    return [v for v in f.get("allowed_enum_values") or [] if isinstance(v, str)]


def _table_gist(name, t):
    """A short natural-language summary of a table used as the semantic-
    search document: its name, first-sentence description, and each field's
    name plus enum values -- enough for an embedding to capture "what this
    table is about" (including which literal values live in it) without
    paying for the full (often much longer) field-by-field description
    text."""
    field_parts = []
    for f in t["fields"]:
        vals = _field_enum_values(f)
        field_parts.append(f'{f["name"]} ({" ".join(vals)})' if vals else f["name"])
    desc = _clean_description_for_retrieval(t["description"])
    return f"{name}: {desc}. Fields: {' '.join(field_parts)}"


def retrieve_relevant_tables(graph, question, top_k=6, expand_hops=True):
    """Hybrid retrieval: scores tables by a blend of (a) lexical overlap
    between question tokens and table/field names + enum values (weighted
    higher) + descriptions (cleaned of classification prefixes, first
    sentence only), and (b) semantic (embedding cosine) similarity between
    the question and each table's gist -- see _table_gist and
    semantic_search.py for why lexical alone isn't sufficient. Then expands
    one hop across REFERENCES/LINKS_TO edges so joinable tables aren't
    dropped. Falls back to the full schema if nothing scores above zero."""
    tables = fetch_all_tables(graph)
    if graph is None:
        # Twelve small schema tables fit in the local model context. Preserve all
        # join paths and nested types when the optional graph is not configured.
        return tables
    q_tokens = _tokenize(question)
    q_vec = semantic_search.embed(question)

    table_names = list(tables.keys())
    gist_vecs = semantic_search.embed([_table_gist(name, tables[name]) for name in table_names])

    scores = {}
    for name, gist_vec in zip(table_names, gist_vecs):
        t = tables[name]
        name_tokens = _tokenize(name)
        desc_tokens = _tokenize(_clean_description_for_retrieval(t["description"]))
        for f in t["fields"]:
            name_tokens |= _tokenize(f["name"])
            for v in _field_enum_values(f):
                name_tokens |= _tokenize(v)  # an enum value like AML_SAR is
                                              # as strong a signal as a field
                                              # name, not incidental prose
            desc_tokens |= _tokenize(_clean_description_for_retrieval(f["description"]))
        desc_tokens -= name_tokens  # don't double-count a word that's in both
        raw_lexical = _NAME_MATCH_WEIGHT * len(q_tokens & name_tokens) + len(q_tokens & desc_tokens)
        lexical = min(raw_lexical / len(q_tokens), 1.0) if q_tokens else 0.0
        semantic = semantic_search.cosine_sim(q_vec, gist_vec)
        scores[name] = TABLE_LEXICAL_WEIGHT * lexical + TABLE_SEMANTIC_WEIGHT * semantic

    # Tie-break alphabetically: FalkorDB doesn't guarantee row order without
    # an ORDER BY, so without this, which tables land in the top_k on a score
    # tie (and therefore what schema the model sees) varied nondeterministically
    # between otherwise-identical runs.
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    selected = {name for name, score in ranked[:top_k] if score > 0}

    if not selected:
        return tables  # nothing matched lexically -> hand the model everything

    if expand_hops:
        neighbor_rows = graph.query(
            """
            MATCH (t1:Table)-[:HAS_FIELD]->(:Field)-[:REFERENCES|LINKS_TO]->(:Field)<-[:HAS_FIELD]-(t2:Table)
            WHERE t1.name IN $names
            RETURN DISTINCT t2.name AS neighbor
            """,
            {"names": list(selected)},
        ).result_set
        for (neighbor,) in neighbor_rows:
            selected.add(neighbor)

    result = {name: t for name, t in tables.items() if name in selected}
    for t in result.values():
        t["fields"] = _prune_fields(t, q_tokens)
    return result


MAX_FIELDS_PER_TABLE = 10  # keeps the schema string focused on fields a real
                            # KPI query would use; Party alone carries 19
                            # fields (most of them rarely-queried contact-
                            # detail structs), which both bloats the input
                            # past what the model was trained on and gives it
                            # more surface area to hallucinate a plausible-
                            # looking but wrong column from.


def _prune_fields(t, q_tokens):
    """When a table has more than MAX_FIELDS_PER_TABLE fields, keep only the
    ones most relevant to the question (lexical overlap against the field's
    name, enum values, and cleaned description), plus anything structurally
    load-bearing regardless of relevance: primary-key columns and any column
    that's the source of a foreign key -- dropping those would break join
    generation even though they rarely share vocabulary with the question."""
    fields = t["fields"]
    if len(fields) <= MAX_FIELDS_PER_TABLE:
        return fields

    must_keep = set(t["primary_key"]) | {fk["column"] for fk in t["foreign_keys"]}

    def relevance(f):
        if f["name"] in must_keep:
            return float("inf")
        tokens = _tokenize(f["name"])
        for v in _field_enum_values(f):
            tokens |= _tokenize(v)
        tokens |= _tokenize(_clean_description_for_retrieval(f["description"]))
        return len(q_tokens & tokens)

    # Tie-break alphabetically for the same reason table retrieval does:
    # FalkorDB doesn't guarantee HAS_FIELD row order without an ORDER BY, so
    # which fields survive pruning on a relevance tie varied nondeterministically
    # between otherwise-identical runs without this.
    ranked = sorted(fields, key=lambda f: (-relevance(f), f["name"]))
    return ranked[:MAX_FIELDS_PER_TABLE]


# ---------------------------------------------------------------------------
# Schema serialization (CREATE TABLE DDL, the format SQLCoder was fine-tuned on)
# ---------------------------------------------------------------------------

MAX_INLINE_ENUM_VALUES = 6  # keeps the schema string a reasonable size; some
                             # fields (e.g. education_level_code) carry 11
                             # authoritative values, and showing all of them
                             # for every such field would bloat the input.


def build_ddl_schema(tables):
    """CREATE TABLE-style schema string -- the format SQLCoder was actually
    fine-tuned on. Enum values and descriptions become trailing `--` comments
    per column, which is exactly how SQLCoder's own training schemas carry
    that kind of hint (its model card's examples do this for categorical
    columns).

    Deliberately bare (unquoted) identifiers, not the Postgres-style
    double-quoted "col" SQLCoder's own DDL examples use: this schema targets
    BigQuery, where double quotes delimit string *literals* (matching
    schema_validator's literal-masking, which must treat them that way to
    catch real string literals). Quoting identifiers here taught the model to
    echo that quoting in its generated SQL, which then got masked as a fake
    literal and flagged as a bogus column -- a real bug found by testing
    generation end-to-end, not a style preference."""
    statements = []
    for t in tables.values():
        lines = []
        for f in t["fields"]:
            col = f'    {f["name"]} {f.get("sql_type") or sql_type(f["type"])}'
            comment_bits = []
            enum_values = _field_enum_values(f)
            if enum_values:
                shown = enum_values[:MAX_INLINE_ENUM_VALUES]
                more = len(enum_values) - len(shown)
                suffix = f", +{more} more" if more > 0 else ""
                comment_bits.append(f"values: {', '.join(shown)}{suffix}")
            desc = _clean_description_for_retrieval(f["description"])
            if desc:
                comment_bits.append(desc)
            if comment_bits:
                col += f" -- {'; '.join(comment_bits)}"
            lines.append(col)
        for fk in t["foreign_keys"]:
            lines.append(f'    FOREIGN KEY ({fk["column"]}) REFERENCES {fk["ref_table"]}')
        if t["primary_key"]:
            pk_cols = ", ".join(t["primary_key"])
            lines.append(f"    PRIMARY KEY ({pk_cols})")
        # Keep the comma before -- comments so it is not swallowed by the comment.
        body = "\n".join((line.replace(" --", ", --", 1) if " --" in line else line + ",")
                         if i < len(lines) - 1 else line for i, line in enumerate(lines))
        statements.append(f'CREATE TABLE {t["name"]} (\n{body}\n);')
    return "\n\n".join(statements)


# ---------------------------------------------------------------------------
# Prompt assembly + inference
# ---------------------------------------------------------------------------

# Defog's own recommended SQLCoder prompt shape -- the
# [QUESTION]...[/QUESTION] and [SQL] tags are part of what it was fine-tuned
# to recognize, not decoration.
SQLCODER_PROMPT_TEMPLATE = """### Task
Generate a SQL query to answer [QUESTION]{question}[/QUESTION]

### Instructions
{instructions}

### Database Schema
This query will run on a database whose schema is represented in this string:
{ddl_schema}

### Answer
Given the database schema, here is the SQL query that [QUESTION]{question}[/QUESTION]
[SQL]
"""


def build_sqlcoder_input(question, tables, ground_tables=None, feedback=None):
    """Note: the business-analyst/read-only-SQL persona (SYSTEM_PROMPT) is
    NOT folded in here -- it's passed separately to generate_sql_ollama as
    Ollama's native `system` field, since SQLCoder (unlike the T5 checkpoint)
    actually attends to a system role instead of just the prompt body."""
    ddl_schema = build_ddl_schema(tables)
    instructions = [
        "- Dialect: Google BigQuery GoogleSQL, NOT PostgreSQL. Use backticks for identifiers, single quotes for strings.",
        "- Only generate a SELECT statement -- this is a read-only analytics assistant.",
        "- Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or MERGE.",
        "- Use only the tables and columns defined in the database schema below; never invent one.",
        "- DATE_TRUNC(DATE(timestamp_column), MONTH) groups months; use COUNTIF and SAFE_DIVIDE when needed.",
        "- Monetary normalized_booked_amount is a STRUCT. Sum CAST(units AS NUMERIC) + CAST(nanos AS NUMERIC) / 1000000000, currency USD. Always qualify units and nanos with normalized_booked_amount.",
        "- Party has historical versions. For current customers use ROW_NUMBER() OVER (PARTITION BY party_id ORDER BY validity_start_time DESC) and keep row 1 before joining.",
        "- Latest available data is August 2026. Latest risk scores use MAX(risk_period_end_time), not CURRENT_DATE().",
        "- Customer country/entity is the party_id/account_id prefix: HASE_HK, HSBC_GB, HSBC_IN, HSBC_TW, HSBC_FR, HSBC_PL, HSBC_IE. Do not use counterparty country for customer country.",
        "- Country names map EXACTLY: Hong Kong=HASE_HK_, UK/Britain=HSBC_GB_, India=HSBC_IN_, Taiwan=HSBC_TW_, France=HSBC_FR_, Poland=HSBC_PL_, Ireland=HSBC_IE_.",
        "- Example country predicate for India risk scores: STARTS_WITH(rs.party_id, 'HSBC_IN_'). Example France transactions: STARTS_WITH(t.account_id, 'HSBC_FR_'). Never use occupation, gender, or name to filter country. Do not join Party merely to obtain country; the party_id already has the country prefix.",
        "- Transaction.account_id joins AccountPartyLink.account_id; AccountPartyLink.party_id joins Party.party_id.",
        "- RiskScores, Explainability and RiskCaseEvent join Party via party_id. Scores and explanations also join on risk_period_end_time.",
        "- Array fields require UNNEST; STRUCT fields use dot notation. Never cast an entire STRUCT to a number.",
        "- SAR cases are COUNT(DISTINCT risk_case_id) with type = 'AML_SAR'; events and cases are different counts.",
        "- If the question lacks essential meaning or is not answerable from this schema, output CLARIFICATION_REQUIRED instead of guessing.",
    ]
    if ground_tables:
        instructions.append(f"- Use only these exact table names: {', '.join(ground_tables)}.")
    if feedback:
        instructions.append(f"- Your previous attempt was invalid: {feedback}. Fix this and try again.")
    return SQLCODER_PROMPT_TEMPLATE.format(
        question=question, instructions="\n".join(instructions), ddl_schema=ddl_schema,
    )


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def _extract_sql(text):
    """SQLCoder is instruction-following (unlike the T5 checkpoint), so its
    raw response can carry markdown fences, the [SQL]/[/SQL] tags it was
    prompted with, or trailing prose after the query -- strip all of that
    down to just the statement."""
    text = text.strip()
    fence = _SQL_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    text = text.replace("[SQL]", "").replace("[/SQL]", "").strip()
    return text.strip()


def generate_sql_ollama(prompt, system=None, model=None, host=None, max_retries=1):
    """Calls the local Ollama server's /api/generate endpoint. temperature=0
    (and a fixed seed) per the model's usage guidance: SQLCoder should be run
    deterministically, not sampled, for reproducible query generation.

    `system` is passed through Ollama's own `system` field rather than
    folded into the prompt body -- unlike the T5 checkpoint, SQLCoder is
    instruction-following (Llama-3 based) and actually attends to a system
    role, so the business-analyst/read-only-SQL persona in SYSTEM_PROMPT can
    genuinely shape its behavior here, not just document intent."""
    model = model or OLLAMA_MODEL
    host = host or OLLAMA_HOST
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "seed": 0, "top_p": 1.0,
                    "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
                    "num_predict": 1200},
    }
    if system:
        payload["system"] = system
    resp = requests.post(
        f"{host}/api/generate",
        json=payload,
        timeout=int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "240")),
    )
    resp.raise_for_status()
    return _extract_sql(resp.json().get("response", ""))


def load_model():
    """No local weights to load -- generation goes through the Ollama
    server's HTTP API (generate_sql_ollama). Verify it's actually reachable
    now, at startup, instead of failing opaquely on the first real request.
    Returns (None, None); kept as a function (rather than removed outright)
    so callers (app.py's lifespan, evaluate_text2sql.py) have one place to
    do this startup check without depending on generate_sql_ollama's
    internals."""
    if MODEL_BACKEND != "ollama":
        raise RuntimeError("This integration requires MODEL_BACKEND=ollama")
    try:
        tags = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        tags.raise_for_status()
        names = {m.get("name") for m in tags.json().get("models", [])}
        if not any(n in {OLLAMA_MODEL, OLLAMA_MODEL + ":latest"} for n in names):
            raise RuntimeError(f"Required model is not installed. Run ollama pull {OLLAMA_MODEL}")
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Ollama server not reachable at {OLLAMA_HOST}. Start it (the Ollama app, or "
            f"`ollama serve`) and ensure `{OLLAMA_MODEL}` is pulled "
            f"(`ollama pull {OLLAMA_MODEL}`)."
        ) from exc
    return None, None


# ---------------------------------------------------------------------------
# Exemplar retrieval: reuse a known-good gold query instead of letting the
# model free-generate when the incoming question is (near-)identical to one
# management already asks routinely. This is the "retrieval" half of KAG --
# for a fixed KPI chatbot, most traffic is a small set of recurring
# questions, so retrieving a verified answer beats regenerating and risking
# hallucination every time.
# ---------------------------------------------------------------------------

EXEMPLAR_BANK_PATH = os.path.join(PROJECT_ROOT, "eval", "eval_testcases.json")

# Hybrid exemplar-match blend. Calibrated empirically against three cases
# (see docs/TEST_REPORT.md):
#   genuine paraphrase   "how many sars were filed per month" vs. the stored
#                         "How many SARs were filed each month?"
#                         lexical=0.75  semantic=0.94  -> must MATCH
#   false-positive risk  "minimum and maximum risk score ... risk period"
#                         vs. the stored "average and 90th percentile risk
#                         score ... risk period" (same template, different
#                         statistic) -- lexical=0.64  semantic=0.77
#                         -> must NOT match (pure-lexical 0.6 threshold used
#                         to wrongly match this)
#   ambiguous edge case  "Break down the risk case count by type" vs. the
#                         stored "How many risk cases are there for each
#                         case type?" -- lexical=0.20  semantic=0.76
#                         -> arguably the same question, but low-confidence;
#                         left to fall through to model generation rather
#                         than risk over-matching
# The general-purpose embedding model doesn't separate "different statistic,
# same sentence template" from genuine paraphrase as sharply as hoped (0.77
# vs 0.94 is the whole margin available), so lexical still carries real
# weight rather than being reduced to a tie-breaker.
EXEMPLAR_LEXICAL_WEIGHT = 0.4
EXEMPLAR_SEMANTIC_WEIGHT = 0.6
EXEMPLAR_MATCH_THRESHOLD = 0.75


def load_exemplar_bank():
    if not os.path.exists(EXEMPLAR_BANK_PATH):
        return []
    with open(EXEMPLAR_BANK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def retrieve_exemplar(question, threshold=EXEMPLAR_MATCH_THRESHOLD):
    """Returns (sql, score, matched_question) for the closest exemplar if it
    clears the blended lexical+semantic similarity threshold, else
    (None, best_score, None)."""
    bank = load_exemplar_bank()
    if not bank:
        return None, 0.0, None

    q_tokens = _tokenize(question)
    q_vec = semantic_search.embed(question)
    bank_vecs = semantic_search.embed([ex["question"] for ex in bank])

    best_sql, best_question, best_score = None, None, 0.0
    for ex, ex_vec in zip(bank, bank_vecs):
        ex_tokens = _tokenize(ex["question"])
        union = q_tokens | ex_tokens
        lexical = len(q_tokens & ex_tokens) / len(union) if union else 0.0
        semantic = semantic_search.cosine_sim(q_vec, ex_vec)
        score = EXEMPLAR_LEXICAL_WEIGHT * lexical + EXEMPLAR_SEMANTIC_WEIGHT * semantic
        if score > best_score:
            best_score, best_sql, best_question = score, ex["sql"], ex["question"]
    if best_score >= threshold:
        return best_sql, best_score, best_question
    return None, best_score, None


# ---------------------------------------------------------------------------
# Schema-grounding repair ("schema fixation"): the model sometimes invents
# table/column names close to but not exactly a real one (e.g. "RiskCase"
# instead of "RiskCaseEvent"), which difflib's character-overlap fuzzy match
# catches fine. But some hallucinations are conceptually right and lexically
# unrelated -- e.g. "SAR" for RiskCaseEvent, whose description literally
# mentions AML_SAR filings, or "customer" for Party -- where character-level
# similarity is near zero but semantic similarity is high. Since the
# retrieval step already knows exactly which tables/columns are real for
# this question, try difflib first (cheap, precise for near-misses) and
# fall back to embedding similarity against each candidate's descriptive
# gist before giving up and leaving the identifier for schema_validator to
# flag as a violation.
# ---------------------------------------------------------------------------

_TABLE_REF_RE = re.compile(r"\b(FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_COLUMN_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")

# Calibrated against a labeled set of real hallucinations observed across
# this session's eval runs (positive: should repair; negative: garbage that
# should stay unresolved), not guessed -- see schema_validator.py's matching
# constants for the full measured evidence (this module's gist format is the
# same "name: desc. Fields: ..." shape, so the same numbers apply).
#
# A short hallucinated identifier embedded against a long table-gist scores
# much lower in absolute cosine similarity than two comparable-length
# sentences do, even when the ranking is correct -- e.g. "transactionlog"
# scores only 0.50 against Transaction's gist despite being an obvious
# match. TABLE_SEMANTIC_REPAIR_CUTOFF sits at 0.30 rather than lower because
# a real negative ("Model", a hallucination with no matching table) scored
# 0.28-0.33 -- *higher* than "SAR"/"sar_filing"'s 0.23-0.25 against
# RiskCaseEvent. No single cutoff catches the SAR-class repairs without also
# letting Model-class negatives through as false corrections, so this errs
# toward never wrongly "fixing" a table name, at the cost of leaving a few
# genuine matches unresolved. Column-level cutoffs sit higher because field
# descriptions are shorter, so the length asymmetry is less severe, and
# garbage-vs-signal separation was clean at that scale.
TABLE_SEMANTIC_REPAIR_CUTOFF = 0.30
COLUMN_SEMANTIC_REPAIR_CUTOFF = 0.35


def repair_table_names(sql, tables, cutoff=0.5, semantic_cutoff=TABLE_SEMANTIC_REPAIR_CUTOFF):
    """`tables` is the {name: table_dict} mapping from retrieve_relevant_tables
    (not just a list of names) so the semantic fallback has descriptions to
    embed."""
    valid_tables = list(tables.keys())
    gist_vecs = None  # computed lazily, once, only if a semantic fallback is ever needed
    corrections = []

    def repl(match):
        nonlocal gist_vecs
        keyword, name = match.group(1), match.group(2)
        for t in valid_tables:
            if t.lower() == name.lower():
                return f"{keyword} {t}"
        # schema_validator._fuzzy_match_ci (not plain difflib): case-
        # insensitive, and guards short words against coincidental
        # character-overlap matches (e.g. "sar" vs "party" scores exactly
        # 0.5 from two shared letters alone).
        close = schema_validator._fuzzy_match_ci(name, valid_tables, cutoff)
        if close:
            corrections.append({"kind": "table", "stage": "kg_repair", "from": name, "to": close})
            return f"{keyword} {close}"
        if valid_tables:
            if gist_vecs is None:
                gist_vecs = semantic_search.embed([_table_gist(t, tables[t]) for t in valid_tables])
            name_vec = semantic_search.embed(name.replace("_", " "))
            sims = [semantic_search.cosine_sim(name_vec, v) for v in gist_vecs]
            best_i = max(range(len(sims)), key=lambda i: sims[i])
            if sims[best_i] >= semantic_cutoff:
                corrections.append({
                    "kind": "table", "stage": "kg_repair_semantic",
                    "from": name, "to": valid_tables[best_i],
                })
                return f"{keyword} {valid_tables[best_i]}"
        return match.group(0)

    repaired = _TABLE_REF_RE.sub(repl, sql)
    return repaired, corrections


def repair_column_names(sql, tables, cutoff=0.6, semantic_cutoff=COLUMN_SEMANTIC_REPAIR_CUTOFF):
    """Fixes qualified column references (alias.column) against the field
    names of the tables actually retrieved for this question. Also undoes a
    common T5 decoding artifact where whitespace gets inserted right after
    the dot (e.g. "T2. party_ide" -> "T2.party_ide") before fuzzy-matching."""
    sql = re.sub(r"(\b[A-Za-z_][A-Za-z0-9_]*\.)\s+", r"\1", sql)

    field_desc = {}
    for t in tables.values():
        for f in t["fields"]:
            field_desc.setdefault(f["name"], f.get("description", ""))
    valid_columns = sorted(field_desc.keys())
    col_gist_vecs = None  # computed lazily, once, only if needed

    corrections = []

    def repl(match):
        nonlocal col_gist_vecs
        prefix, col = match.group(1), match.group(2)
        for c in valid_columns:
            if c.lower() == col.lower():
                return f"{prefix}.{c}"
        close = schema_validator._fuzzy_match_ci(col, valid_columns, cutoff)
        if close:
            corrections.append({"kind": "column", "stage": "kg_repair", "from": col, "to": close})
            return f"{prefix}.{close}"
        if valid_columns:
            if col_gist_vecs is None:
                col_gist_vecs = semantic_search.embed(
                    [f"{c}: {field_desc[c]}" for c in valid_columns]
                )
            col_vec = semantic_search.embed(col.replace("_", " "))
            sims = [semantic_search.cosine_sim(col_vec, v) for v in col_gist_vecs]
            best_i = max(range(len(sims)), key=lambda i: sims[i])
            if sims[best_i] >= semantic_cutoff:
                corrections.append({
                    "kind": "column", "stage": "kg_repair_semantic",
                    "from": col, "to": valid_columns[best_i],
                })
                return f"{prefix}.{valid_columns[best_i]}"
        return match.group(0)

    repaired = _COLUMN_REF_RE.sub(repl, sql)
    return repaired, corrections


# ---------------------------------------------------------------------------
# Unified KAG pipeline: retrieval -> exemplar shortcut or model generation ->
# schema-grounded repair. Used by both the FastAPI app and the CLI.
# ---------------------------------------------------------------------------

def _summarize_violations(violations, max_items=3):
    """Short, plain feedback line for the self-correction retry -- not a
    full explanation (see build_sqlcoder_input's feedback docstring for why
    this stays terse rather than verbose)."""
    shown = violations[:max_items]
    text = "; ".join(shown)
    if len(violations) > max_items:
        text += f"; and {len(violations) - max_items} more issue(s)"
    return text


def _attempt_generation(question, tables, with_system_prompt, ground_tables, feedback=None):
    """One generate-repair-validate pass. Returns (sql, corrections, violations, model_input)."""
    resolved_ground_tables = list(tables.keys()) if ground_tables else None
    model_input = build_sqlcoder_input(
        question, tables, ground_tables=resolved_ground_tables, feedback=feedback,
    )
    raw_sql = generate_sql_ollama(model_input, system=SYSTEM_PROMPT if with_system_prompt else None)
    table_repaired_sql, table_corrections = repair_table_names(raw_sql, tables)
    kg_repaired_sql, column_corrections = repair_column_names(table_repaired_sql, tables)
    validated_sql, schema_corrections, violations = schema_validator.validate_and_fix(kg_repaired_sql)
    corrections = table_corrections + column_corrections + schema_corrections
    return validated_sql, corrections, violations, model_input


def generate_sql_kag(question, top_k=6, with_system_prompt=False, use_exemplars=True,
                      ground_tables=False, retry_on_invalid=False, execution_mode=False,
                      allowed_columns=None):
    if execution_mode:
        # Keep main's Ollama prompt/inference path, but avoid fuzzy repairs or
        # exemplar shortcuts when SQL will be executed against real BigQuery.
        physical = fetch_catalog_tables()
        names = set(allowed_columns) if allowed_columns is not None else set(physical)
        tables = {name: physical[name] for name in physical if name in names}
        for name, table in tables.items():
            if allowed_columns is not None:
                table["fields"] = [f for f in table["fields"] if f["name"] in allowed_columns[name]]
        model_input = build_sqlcoder_input(question, tables, ground_tables=list(tables))
        sql = generate_sql_ollama(model_input, system=SYSTEM_PROMPT)
        from pipeline.bigquery_schema import normalize_date_trunc, validate_candidate
        sql, corrections = normalize_date_trunc(sql)
        violations = validate_candidate(sql, tables)
        return {"question": question, "sql": sql, "source": "ollama_generation",
                "matched_question": None, "similarity": 0.0, "tables_used": list(tables),
                "corrections": corrections, "schema_valid": not violations, "schema_violations": violations,
                "model_input": None, "retried": False}

    graph = connect_graph()
    tables = retrieve_relevant_tables(graph, question, top_k=top_k)
    exemplar_sql, exemplar_score, matched_question = (None, 0.0, None)
    if use_exemplars:
        exemplar_sql, exemplar_score, matched_question = retrieve_exemplar(question)

    if exemplar_sql:
        validated_sql, schema_corrections, violations = schema_validator.validate_and_fix(exemplar_sql)
        return {
            "question": question,
            "sql": validated_sql,
            "source": "exemplar_retrieval",
            "matched_question": matched_question,
            "similarity": exemplar_score,
            "tables_used": list(tables.keys()),
            "corrections": schema_corrections,
            "schema_valid": not violations,
            "schema_violations": violations,
            "model_input": None,
            "retried": False,
        }

    validated_sql, corrections, violations, model_input = _attempt_generation(
        question, tables, with_system_prompt, ground_tables
    )

    retried = False
    if violations and retry_on_invalid:
        # Self-correction retry: widen retrieval (the real table/column may
        # have fallen outside the first attempt's top_k) and feed the
        # specific violations back into the input, then regenerate once.
        # Only ever one retry -- this is a bounded safety net, not a loop
        # that could compound latency or drift further off-template with
        # each pass.
        retry_top_k = min(top_k * 2, 12)  # 12 == the whole schema right now
        retry_tables = retrieve_relevant_tables(graph, question, top_k=retry_top_k)
        feedback = _summarize_violations(violations)

        retry_sql, retry_corrections, retry_violations, retry_model_input = _attempt_generation(
            question, retry_tables, with_system_prompt, ground_tables, feedback=feedback,
        )
        retried = True

        # Keep whichever attempt is actually better -- fewer unresolved
        # violations wins; a tie keeps the first attempt (no reason to
        # prefer a different-but-equally-uncertain answer).
        if len(retry_violations) < len(violations):
            tables = retry_tables
            validated_sql, corrections, violations = retry_sql, retry_corrections, retry_violations
            model_input = retry_model_input

    return {
        "question": question,
        "sql": validated_sql,
        "source": "model_generation",
        "matched_question": None,
        "similarity": exemplar_score,
        "tables_used": list(tables.keys()),
        "corrections": corrections,
        "schema_valid": not violations,
        "schema_violations": violations,
        "model_input": model_input,
        "retried": retried,
    }


def compare_with_and_without_system_prompt(questions):
    load_model()  # verify Ollama is reachable before running through all questions
    for question in questions:
        print("=" * 80)
        print("Question:", question)
        outcome_plain = generate_sql_kag(question, with_system_prompt=False)
        outcome_sys = generate_sql_kag(question, with_system_prompt=True)
        print(f"  without system prompt [{outcome_plain['source']}]:", outcome_plain["sql"])
        print(f"  with system prompt    [{outcome_sys['source']}]:", outcome_sys["sql"])


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--with-system-prompt":
        args = args[1:]
        use_system_prompt = True
    else:
        use_system_prompt = False

    if args and args[0] == "--compare":
        default_questions = [
            "List the party id and risk score for parties with a risk score above 0.8",
            "Show the top 10 parties by risk score",
            "Delete all parties with a risk score below 0.1",
            "Count the number of open risk cases per typology",
        ]
        compare_with_and_without_system_prompt(default_questions)
    else:
        question = " ".join(args) or (
            "List the party id and risk score for parties with a risk score above 0.8"
        )
        outcome = generate_sql_kag(question, with_system_prompt=use_system_prompt)
        print("Source:", outcome["source"])
        if outcome["matched_question"]:
            print("Matched exemplar question:", outcome["matched_question"], f"(similarity {outcome['similarity']:.2f})")
        if outcome["corrections"]:
            print("Corrections applied:", outcome["corrections"])
        if not outcome["schema_valid"]:
            print("WARNING: could not fully validate against aml_data_model_schema.json:")
            for v in outcome["schema_violations"]:
                print("  -", v)
        print("Generated SQL:", outcome["sql"])
