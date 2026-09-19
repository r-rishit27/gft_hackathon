"""
Text-to-SQL over the AML knowledge graph stored in FalkorDB.

Pipeline:
  1. Retrieval engine: given a natural-language question, pull the most
     relevant tables/fields/constraints/foreign-keys out of the FalkorDB
     knowledge graph (aml_data_model) instead of hardcoding a schema.
  2. Schema serialization: format the retrieved tables into the
     "table col type , col type ... foreign_key: ... primary key: ... [SEP] ..."
     string that gaussalgo/T5-LM-Large-text2sql-spider was fine-tuned on.
  3. Prompt assembly: SYSTEM_PROMPT documents the task; the user's question
     plus the retrieved schema are combined into the exact
     "Question: <q> Schema: <schema>" input format the model expects.
  4. Inference: run the HF model to generate a SQL query.

Note: this T5 checkpoint is a seq2seq text2sql model fine-tuned on a fixed
"Question: ... Schema: ..." template -- it is not an instruction-following
chat model. SYSTEM_PROMPT is kept separate (used for logging / documenting
intent, and as a place to inject task-level instructions if the model is
later swapped for an instruction-tuned LLM); it is NOT concatenated into the
model input, since doing so would push the input outside the format the
model was trained on and degrade generation quality.
"""

import os
import re
import sys

from dotenv import load_dotenv
from falkordb import FalkorDB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

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

def _tokenize(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


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

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
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

def format_table(table):
    parts = [table["name"]]
    col_str = " , ".join(f'"{f["name"]}" {sql_type(f["type"])}' for f in table["fields"])
    parts.append(col_str)
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

def build_model_input(question, schema_string, with_system_prompt=False):
    if with_system_prompt:
        return " ".join([SYSTEM_PROMPT, "Question: ", question, "Schema:", schema_string])
    return " ".join(["Question: ", question, "Schema:", schema_string])


def load_model():
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, token=HF_TOKEN)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_PATH, token=HF_TOKEN)
    return tokenizer, model


def generate_sql(tokenizer, model, model_input, max_length=512):
    inputs = tokenizer(model_input, return_tensors="pt", truncation=True, max_length=max_length)
    outputs = model.generate(**inputs, max_length=max_length)
    return tokenizer.batch_decode(outputs, skip_special_tokens=True)[0]


def answer_question(question, top_k=6, verbose=True, with_system_prompt=False,
                     tokenizer=None, model=None):
    graph = connect_graph()
    tables = retrieve_relevant_tables(graph, question, top_k=top_k)
    schema_string = build_schema_string(tables)
    model_input = build_model_input(question, schema_string, with_system_prompt=with_system_prompt)

    if verbose:
        print("System prompt:", SYSTEM_PROMPT if with_system_prompt else "(not sent to model)")
        print("Retrieved tables:", ", ".join(tables.keys()))
        print("Model input:", model_input)
        print()

    if tokenizer is None or model is None:
        tokenizer, model = load_model()
    sql = generate_sql(tokenizer, model, model_input)
    return sql


def compare_with_and_without_system_prompt(questions):
    tokenizer, model = load_model()
    for question in questions:
        print("=" * 80)
        print("Question:", question)
        sql_plain = answer_question(
            question, verbose=False, with_system_prompt=False, tokenizer=tokenizer, model=model
        )
        sql_sys = answer_question(
            question, verbose=False, with_system_prompt=True, tokenizer=tokenizer, model=model
        )
        print("  without system prompt:", sql_plain)
        print("  with system prompt:   ", sql_sys)


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
        sql = answer_question(question, with_system_prompt=use_system_prompt)
        print("Generated SQL:", sql)
