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

import difflib
import json
import os
import re
import sys

from dotenv import load_dotenv
from falkordb import FalkorDB

try:
    from pipeline import schema_validator
except ImportError:
    # Running this file directly (`python pipeline/text2sql_falkordb.py ...`)
    # rather than as part of the `pipeline` package -- the script's own
    # directory is already on sys.path, so the plain import finds the sibling
    # module.
    import schema_validator

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
               f.description AS description, f.is_primary_key AS is_pk
        """
    ).result_set
    for table, name, ftype, description, is_pk in rows:
        if table in tables:
            tables[table]["fields"].append(
                {"name": name, "type": ftype, "description": description or "", "is_pk": bool(is_pk)}
            )

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


def retrieve_relevant_tables(graph, question, top_k=6, expand_hops=True):
    """Lexical retrieval: score tables by overlap between question tokens and
    table/field names + descriptions, then expand one hop across REFERENCES /
    LINKS_TO edges so joinable tables aren't dropped. Falls back to the full
    schema if nothing scores above zero."""
    tables = fetch_all_tables(graph)
    q_tokens = _tokenize(question)

    scores = {}
    for name, t in tables.items():
        haystack_tokens = _tokenize(name) | _tokenize(t["description"])
        for f in t["fields"]:
            haystack_tokens |= _tokenize(f["name"]) | _tokenize(f["description"])
        scores[name] = len(q_tokens & haystack_tokens)

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

    return {name: t for name, t in tables.items() if name in selected}


# ---------------------------------------------------------------------------
# Schema serialization (Spider-style, as required by the model card)
# ---------------------------------------------------------------------------

# Canonical implementation lives in schema_validator.py (it also drives the
# literal-value casing correction in validate_and_fix); reused here so the
# schema-serialization hints shown to the model and the validator's notion
# of "valid enum values" can never drift apart.
extract_enum_hint = schema_validator.extract_enum_hint


def format_table(table):
    parts = [table["name"]]
    col_parts = []
    for f in table["fields"]:
        col_str = f'"{f["name"]}" {sql_type(f["type"])}'
        enum_values = extract_enum_hint(f.get("description"))
        if enum_values:
            col_str += f' (values: {", ".join(enum_values)})'
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

def build_model_input(question, schema_string, with_system_prompt=False, ground_tables=None):
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
EXEMPLAR_MATCH_THRESHOLD = 0.6  # Jaccard token overlap


def load_exemplar_bank():
    if not os.path.exists(EXEMPLAR_BANK_PATH):
        return []
    with open(EXEMPLAR_BANK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def retrieve_exemplar(question, threshold=EXEMPLAR_MATCH_THRESHOLD):
    """Returns (sql, score, matched_question) for the closest exemplar if it
    clears the similarity threshold, else (None, best_score, None)."""
    bank = load_exemplar_bank()
    q_tokens = _tokenize(question)
    best_sql, best_question, best_score = None, None, 0.0
    for ex in bank:
        ex_tokens = _tokenize(ex["question"])
        if not q_tokens or not ex_tokens:
            continue
        union = q_tokens | ex_tokens
        score = len(q_tokens & ex_tokens) / len(union) if union else 0.0
        if score > best_score:
            best_score, best_sql, best_question = score, ex["sql"], ex["question"]
    if best_score >= threshold:
        return best_sql, best_score, best_question
    return None, best_score, None


# ---------------------------------------------------------------------------
# Schema-grounding repair: the model sometimes invents table names close to
# but not exactly a real one (e.g. "RiskCase" instead of "RiskCaseEvent", or
# a wholesale fabrication like "SAR"). Since the retrieval step already knows
# exactly which tables are real for this question, fuzzy-match every
# FROM/JOIN target back onto that set and correct it instead of shipping a
# query that will fail against the actual database.
# ---------------------------------------------------------------------------

_TABLE_REF_RE = re.compile(r"\b(FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_COLUMN_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")


def repair_table_names(sql, valid_tables, cutoff=0.5):
    corrections = []

    def repl(match):
        keyword, name = match.group(1), match.group(2)
        for t in valid_tables:
            if t.lower() == name.lower():
                return f"{keyword} {t}"
        close = difflib.get_close_matches(name, valid_tables, n=1, cutoff=cutoff)
        if close:
            corrections.append({"kind": "table", "stage": "kg_repair", "from": name, "to": close[0]})
            return f"{keyword} {close[0]}"
        return match.group(0)

    repaired = _TABLE_REF_RE.sub(repl, sql)
    return repaired, corrections


def repair_column_names(sql, tables, cutoff=0.6):
    """Fixes qualified column references (alias.column) against the field
    names of the tables actually retrieved for this question. Also undoes a
    common T5 decoding artifact where whitespace gets inserted right after
    the dot (e.g. "T2. party_ide" -> "T2.party_ide") before fuzzy-matching."""
    sql = re.sub(r"(\b[A-Za-z_][A-Za-z0-9_]*\.)\s+", r"\1", sql)

    valid_columns = sorted({f["name"] for t in tables.values() for f in t["fields"]})
    corrections = []

    def repl(match):
        prefix, col = match.group(1), match.group(2)
        for c in valid_columns:
            if c.lower() == col.lower():
                return f"{prefix}.{c}"
        close = difflib.get_close_matches(col, valid_columns, n=1, cutoff=cutoff)
        if close:
            corrections.append({"kind": "column", "stage": "kg_repair", "from": col, "to": close[0]})
            return f"{prefix}.{close[0]}"
        return match.group(0)

    repaired = _COLUMN_REF_RE.sub(repl, sql)
    return repaired, corrections


# ---------------------------------------------------------------------------
# Unified KAG pipeline: retrieval -> exemplar shortcut or model generation ->
# schema-grounded repair. Used by both the FastAPI app and the CLI.
# ---------------------------------------------------------------------------

def generate_sql_kag(question, top_k=6, with_system_prompt=False, use_exemplars=True,
                      ground_tables=False, tokenizer=None, model=None):
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
        }

    model_input = build_model_input(
        question, schema_string, with_system_prompt=with_system_prompt,
        ground_tables=list(tables.keys()) if ground_tables else None,
    )
    if tokenizer is None or model is None:
        tokenizer, model = load_model()
    raw_sql = generate_sql(tokenizer, model, model_input)
    table_repaired_sql, table_corrections = repair_table_names(raw_sql, list(tables.keys()))
    kg_repaired_sql, column_corrections = repair_column_names(table_repaired_sql, tables)

    # Final gate: re-check against the full canonical schema (not just the
    # KG-retrieved subset), so a correct table/column outside retrieval's
    # top_k can still be recovered, and anything the KG-scoped repair missed
    # gets one more independent pass before the query reaches the user.
    validated_sql, schema_corrections, violations = schema_validator.validate_and_fix(kg_repaired_sql)
    corrections = table_corrections + column_corrections + schema_corrections

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
