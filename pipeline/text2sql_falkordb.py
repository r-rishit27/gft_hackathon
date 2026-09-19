"""
Text-to-SQL over the AML knowledge graph stored in FalkorDB, using a
knowledge-augmented generation (KAG) pipeline: retrieval + a verified-answer
shortcut + schema-grounded repair around the base text2sql model, instead of
fine-tuning it (fine-tuning gaussalgo/T5-LM-Large-text2sql-spider on this
CPU-only box hit memory/time limits and produced an unstable, degenerate
model -- see git history for that attempt).

Pipeline (generate_sql_kag):
  1. Retrieval engine: pull the most relevant tables/fields/constraints/
     foreign-keys out of the FalkorDB knowledge graph (aml_data_model) for
     the question, instead of hardcoding a schema.
  2. Schema serialization: format the retrieved tables into the
     "table col type (values: ...) , ... foreign_key: ... primary key: ...
     [SEP] ..." string that gaussalgo/T5-LM-Large-text2sql-spider expects,
     including enum-value hints pulled verbatim from the KG's own field
     descriptions.
  3. Exemplar retrieval: if the question is a (near-)duplicate of a known,
     verified KPI question (eval_testcases.json), return that gold SQL
     directly -- no model call, no hallucination risk.
  4. Otherwise, generate with the model, then run schema-grounded repair:
     fuzzy-match every FROM/JOIN target back onto the tables retrieval
     actually knows are real for this question, and correct near-misses
     (e.g. "RiskCase" -> "RiskCaseEvent").
  5. Final validation gate (schema_validator.validate_and_fix): re-checks the
     (possibly already-repaired) SQL against the full, canonical
     aml_data_model_schema.json -- not just the KG-retrieved subset -- so a
     correct table/column that fell outside retrieval's top_k selection can
     still be recovered, and anything the KG-scoped repair missed gets a
     second, independent check before the query reaches the user.

Note: this T5 checkpoint is a seq2seq text2sql model fine-tuned on a fixed
"Question: ... Schema: ..." template -- it is not an instruction-following
chat model. SYSTEM_PROMPT is kept separate (used for logging / documenting
intent, and as a place to inject task-level instructions if the model is
later swapped for an instruction-tuned LLM); it is NOT concatenated into the
model input, since doing so would push the input outside the format the
model was trained on and degrade generation quality.
"""

import json
import os
import re
import sys

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

FALKORDB_HOST = os.environ["FALKORDB_HOST"]
FALKORDB_PORT = int(os.environ["FALKORDB_PORT"])
FALKORDB_USERNAME = os.environ["FALKORDB_USERNAME"]
FALKORDB_PASSWORD = os.environ["FALKORDB_PASSWORD"]
FALKORDB_GRAPH = os.environ.get("FALKORDB_GRAPH", "aml_data_model")
HF_TOKEN = os.environ.get("HF_TOKEN")

MODEL_PATH = "gaussalgo/T5-LM-Large-text2sql-spider"

SYSTEM_PROMPT = (
    "You are a business analyst who helps senior management analyze AML "
    "(anti-money-laundering) data for key insights and KPI tracking. Given a "
    "database schema retrieved from a knowledge graph and a question, generate "
    "only read-only SQL: SELECT statements only. Never generate INSERT, UPDATE, "
    "DELETE, DROP, ALTER, TRUNCATE, MERGE, or any other data- or schema-"
    "modifying statement. Use only the tables and columns present in the "
    "schema."
)

# BigQuery-ish types (as stored in the KG) -> Spider-style SQL types the model
# was trained on.
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
# Schema serialization (Spider-style, as required by the model card)
# ---------------------------------------------------------------------------

MAX_INLINE_ENUM_VALUES = 6  # keeps schema-string length in the range the
                             # model was fine-tuned on; some fields (e.g.
                             # education_level_code) now carry 11 authoritative
                             # values, and showing all of them for every such
                             # field would bloat the input well past what was
                             # ever seen in training.


def format_table(table):
    parts = [table["name"]]
    col_parts = []
    for f in table["fields"]:
        col_str = f'"{f["name"]}" {sql_type(f["type"])}'
        enum_values = _field_enum_values(f)
        if enum_values:
            shown = enum_values[:MAX_INLINE_ENUM_VALUES]
            more = len(enum_values) - len(shown)
            suffix = f", +{more} more" if more > 0 else ""
            col_str += f' (values: {", ".join(shown)}{suffix})'
        col_parts.append(col_str)
    parts.append(" , ".join(col_parts))
    for fk in table["foreign_keys"]:
        parts.append(f'foreign_key: {fk["column"]} {fk["type"]} from {fk["ref_table"]}')
    if table["primary_key"]:
        parts.append(f'primary key: {", ".join(table["primary_key"])}')
    return " ".join(parts)


def build_schema_string(tables):
    return " [SEP] ".join(format_table(t) for t in tables.values())


# ---------------------------------------------------------------------------
# Prompt assembly + inference
# ---------------------------------------------------------------------------

def build_model_input(question, schema_string, with_system_prompt=False, ground_tables=None, feedback=None):
    prefix_parts = []
    if with_system_prompt:
        prefix_parts.append(SYSTEM_PROMPT)
    if ground_tables:
        # Dynamic, per-question grounding line (not the static SYSTEM_PROMPT):
        # tells the model exactly which table names are real for *this*
        # question, straight from the retrieval step -- tested empirically
        # against the plain prompt in evaluate_text2sql.py rather than
        # assumed to help (see grounding_ablation.json).
        prefix_parts.append(f"Use only these exact table names: {', '.join(ground_tables)}.")
    if feedback:
        # Self-correction retry line: this checkpoint isn't instruction-
        # following (it's fine-tuned strictly on "Question: ... Schema: ...",
        # like ground_tables above), so this is deliberately short and
        # plain rather than a full natural-language explanation -- more
        # verbiage is more likely to push the input further off the
        # template it was trained on, not less.
        prefix_parts.append(f"Previous attempt was invalid: {feedback}")
    prefix = " ".join(prefix_parts)
    if prefix:
        return " ".join([prefix, "Question: ", question, "Schema:", schema_string])
    return " ".join(["Question: ", question, "Schema:", schema_string])


FINETUNED_MODEL_DIR = os.path.join(PROJECT_ROOT, "training", "finetuned_model")
DEFAULT_MODEL_PATH = FINETUNED_MODEL_DIR if os.path.isdir(FINETUNED_MODEL_DIR) else MODEL_PATH


def load_model(model_path=None):
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    model_path = model_path or DEFAULT_MODEL_PATH
    tokenizer = AutoTokenizer.from_pretrained(model_path, token=HF_TOKEN)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_path, token=HF_TOKEN)
    return tokenizer, model


def generate_sql(tokenizer, model, model_input, max_length=512):
    inputs = tokenizer(model_input, return_tensors="pt", truncation=True, max_length=max_length)
    outputs = model.generate(
        **inputs,
        max_length=max_length,
        no_repeat_ngram_size=3,   # stops the repetition loops the small fine-tune produced
        repetition_penalty=1.3,
    )
    return tokenizer.batch_decode(outputs, skip_special_tokens=True)[0]


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
    full explanation (see build_model_input's feedback docstring for why
    this stays terse rather than verbose)."""
    shown = violations[:max_items]
    text = "; ".join(shown)
    if len(violations) > max_items:
        text += f"; and {len(violations) - max_items} more issue(s)"
    return text


def _attempt_generation(question, tables, schema_string, with_system_prompt, ground_tables,
                         tokenizer, model, feedback=None):
    """One generate-repair-validate pass. Returns (sql, corrections, violations, model_input)."""
    model_input = build_model_input(
        question, schema_string, with_system_prompt=with_system_prompt,
        ground_tables=list(tables.keys()) if ground_tables else None,
        feedback=feedback,
    )
    raw_sql = generate_sql(tokenizer, model, model_input)
    table_repaired_sql, table_corrections = repair_table_names(raw_sql, tables)
    kg_repaired_sql, column_corrections = repair_column_names(table_repaired_sql, tables)
    validated_sql, schema_corrections, violations = schema_validator.validate_and_fix(kg_repaired_sql)
    corrections = table_corrections + column_corrections + schema_corrections
    return validated_sql, corrections, violations, model_input


def generate_sql_kag(question, top_k=6, with_system_prompt=False, use_exemplars=True,
                      ground_tables=False, retry_on_invalid=False, tokenizer=None, model=None):
    graph = connect_graph()
    tables = retrieve_relevant_tables(graph, question, top_k=top_k)
    schema_string = build_schema_string(tables)

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

    if tokenizer is None or model is None:
        tokenizer, model = load_model()

    validated_sql, corrections, violations, model_input = _attempt_generation(
        question, tables, schema_string, with_system_prompt, ground_tables, tokenizer, model
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
        retry_schema_string = build_schema_string(retry_tables)
        feedback = _summarize_violations(violations)

        retry_sql, retry_corrections, retry_violations, retry_model_input = _attempt_generation(
            question, retry_tables, retry_schema_string, with_system_prompt, ground_tables,
            tokenizer, model, feedback=feedback,
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
    tokenizer, model = load_model()
    for question in questions:
        print("=" * 80)
        print("Question:", question)
        outcome_plain = generate_sql_kag(
            question, with_system_prompt=False, tokenizer=tokenizer, model=model
        )
        outcome_sys = generate_sql_kag(
            question, with_system_prompt=True, tokenizer=tokenizer, model=model
        )
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
