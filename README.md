# AML KPI Copilot

A text-to-SQL pipeline for AML (anti-money-laundering) KPI reporting: ask a question in plain English,
generate SQL grounded in the deployed schema, validate it independently, and query the synthetic BigQuery
dataset through a separate read-only analytics service. The original SQL-only endpoint remains available.

## Local live-data integration

Use [the local integration guide](analytics_service/README.md) for Ollama setup, Google login,
approved read-only access provisioning and the free-form results dashboard. The requested model is
`mannix/defog-llama3-sqlcoder-8b`. No public cloud deployment or mock-data fallback is enabled.

See [`docs/TEST_REPORT.md`](docs/TEST_REPORT.md) for detailed evaluation results and known limitations.

## BigQuery demo dataset

The synthetic dataset is deployed at `gen-lang-client-0810987953.aml_demo`
in `asia-south1`: 1,000 retail customers, 50,000 transactions and 12 tables.
Hong Kong identifiers use `HASE_HK`; the other countries use `HSBC_<COUNTRY>`.

- [Deployed dataset schema, enums and observed values](dataset/aml_data_model_schema.json)
- [Dataset documentation and reproducible loading instructions](dataset/README.md)
- [Complete data package, including the migration baseline](dataset/releases/aml_dataset_v2_20260919.zip)
- [Cloud migration verification](dataset/reports/migration_cloud.json)

The enriched schema under `dataset/` documents the actual BigQuery data and is the metadata source
for local execution mode. The original `schema/` document remains available to the legacy model path.
All data and AML outputs in this package are simulated; querying cloud-hosted rows does not make them
real customer data or validated AML predictions.

To restore ignored data files from a fresh checkout, run from the repository root:

```sh
unzip -n dataset/releases/aml_dataset_v2_20260919.zip -d .
python3 -m unittest discover -s dataset -p 'test_*.py'
python3 dataset/check_naming.py
```

The archive includes source copies; `-n` preserves the current checked-in files.

---

## Architecture

```
question
   │
   ▼
retrieve relevant tables/fields  (FalkorDB knowledge graph — hybrid lexical + semantic search)
   │
   ▼
exemplar retrieval ──── (near-)duplicate of a verified KPI question? ──► return its gold SQL, skip generation
   │ no match
   ▼
serialize retrieved schema as CREATE TABLE DDL
   │
   ▼
mannix/defog-llama3-sqlcoder-8b  (local Ollama server, temperature 0 / deterministic)
system prompt: "AML analyst and business leader... strictly adhere to the schema"
   │
   ▼
schema-grounded repair  (fuzzy + semantic match hallucinated table/column names back onto
                          the tables retrieval actually knows are real for this question)
   │
   ▼
final validation gate  (schema_validator.py — re-checks against the full canonical schema
                         file, corrects enum-value casing, catches anything repair missed)
   │
   ▼
validated SQL + violation list, returned to the caller
```

**Why this shape:**
- **Knowledge-graph retrieval, not a hardcoded schema.** The AML data model has 12 tables and hundreds of
  fields; a fixed prompt schema would either omit relevant tables or bloat the model's input on every
  question. Retrieval blends lexical overlap (exact identifier/enum-value hits) with semantic similarity
  (so "customers" still finds `Party`, "SAR" still finds `RiskCaseEvent` via its `type` enum) and expands
  one hop across foreign keys so joinable tables aren't dropped.
- **SQLCoder, not a fine-tuned model.** `mannix/defog-llama3-sqlcoder-8b` runs locally via Ollama and
  needs no fine-tuning. It's instruction-following, so it actually attends to the system prompt's
  read-only/schema-adherence rules. Run deterministically (temperature 0, fixed seed) for reproducible
  query generation. This pipeline originally used a fine-tuned `gaussalgo/T5-LM-Large-text2sql-spider`
  checkpoint; that path (and all training/fine-tuning code) has been fully removed after switching to
  SQLCoder, which matched or beat it on every eval suite without any fine-tuning — see
  [`docs/TEST_REPORT.md` §0](docs/TEST_REPORT.md#0-pivot-fine-tuned-t5--sqlcoder-via-ollama) for the
  before/after numbers and full rationale.
- **Two independent repair passes.** KG-scoped repair (right after generation) has the retrieval step's
  own knowledge of which tables/columns are real for *this* question; the final validation gate re-checks
  against the *entire* canonical schema, so a correct table that fell outside retrieval's top-k selection
  can still be recovered, and anything the first pass missed gets a second, independent check.
- **Exemplar retrieval beats blind regeneration.** A fixed KPI chatbot sees the same handful of questions
  repeatedly; reusing a verified answer for a (near-)duplicate is safer than regenerating and risking a new
  hallucination every time. Novel questions still fall through to model generation.

## Project structure

```
.
├── app.py                    # FastAPI service (entry point) — run with `uvicorn app:app`
├── frontend/
│   └── index.html            # Chat UI served at /ui, talks to app.py's API
├── pipeline/                 # Core text-to-SQL pipeline (importable package)
│   ├── text2sql_falkordb.py  #   retrieval + schema serialization + exemplar shortcut + inference
│   ├── schema_validator.py   #   final validation/repair against the canonical schema file
│   └── semantic_search.py    #   shared sentence-embedding helper used across retrieval/repair
├── graph/
│   └── build_falkordb_graph.py   # Loads schema/aml_data_model_schema.json into FalkorDB as a knowledge graph
├── schema/
│   ├── aml_data_model_schema.json  # Canonical AML input/output data model (tables, fields, types, linkages)
│   └── aml_kpi_queries.sql         # Hand-written reference KPI queries against that schema
├── eval/
│   ├── evaluate_text2sql.py  # Evaluation harness — runs the pipeline against a test file, scores it
│   ├── eval_testcases.json   #   exemplar bank (also used at runtime for exemplar retrieval)
│   ├── eval_heldout.json     #   held-out test set (paraphrases + novel questions)
│   ├── eval_new_queries.json #   additional schema-strict test questions
│   └── *_results.json        #   evaluation output (regenerated by evaluate_text2sql.py)
└── docs/
    ├── TEST_REPORT.md        # Detailed test report: methodology, results, bugs found, limitations
    └── reference/            # Design reference material (not part of the app)
```

## Setup

1. Copy `.env.example` to `.env` and fill in your FalkorDB connection details:
   ```
   FALKORDB_HOST=...
   FALKORDB_PORT=...
   FALKORDB_USERNAME=...
   FALKORDB_PASSWORD=...
   FALKORDB_GRAPH=aml_data_model
   OLLAMA_HOST=http://localhost:11434
   OLLAMA_MODEL=mannix/defog-llama3-sqlcoder-8b
   ```
2. Install dependencies:
   ```
   pip install fastapi "uvicorn[standard]" falkordb python-dotenv requests sentence-transformers
   ```
3. Install [Ollama](https://ollama.com) and pull the SQLCoder model (one-time, ~4.7 GB):
   ```
   ollama pull mannix/defog-llama3-sqlcoder-8b
   ```
   Ollama must be running (the desktop app, or `ollama serve`) whenever the pipeline generates SQL.
4. Load the schema into FalkorDB (one-time, or whenever `schema/aml_data_model_schema.json` changes):
   ```
   python graph/build_falkordb_graph.py
   ```

## Running

**API + chat UI** (from the project root):
```
uvicorn app:app --host 0.0.0.0 --port 8000
```
Then open `http://localhost:8000/ui/` for the chat interface, or `POST /generate-sql` with
`{"question": "..."}` directly. Interactive API docs at `/docs`.

**CLI** (one-off question, no server):
```
python pipeline/text2sql_falkordb.py --with-system-prompt "Show the top 10 parties by risk score"
```

**Evaluate the pipeline** against a test set:
```
python eval/evaluate_text2sql.py --testcases eval/eval_heldout.json --out results.json
```

## Design notes

- **Read-only by construction, not by trust.** The system prompt instructs SELECT-only, read-only
  generation, and `schema_validator.py` independently verifies every table and column reference before a
  query is returned — but neither guarantees the query is *correct*, only that it's *safe to attempt*.
  Always review generated SQL before running it.
- **Exemplar retrieval over blind generation.** For a fixed set of recurring KPI questions, reusing a
  verified answer beats regenerating and risking hallucination every time. Novel questions fall through to
  model generation, which is repaired and validated the same way.
- **No result fabrication.** This pipeline generates SQL; it does not execute it or know what the data
  actually contains. The UI never shows numbers, charts, or KPIs that weren't computed from a real query
  result — see `docs/TEST_REPORT.md` §5 for exactly what schema validation does and doesn't guarantee.
