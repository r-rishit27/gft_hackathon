# Local Ollama + BigQuery Analytics

Free-form question -> local SQLCoder -> independent GoogleSQL validator -> BigQuery authorized views -> deterministic dashboard. Runtime results come from `gen-lang-client-0810987953.aml_demo` in `asia-south1`: actual cloud-hosted **synthetic demo data**, not real banking customer data. No fixture fallback or cached KPI-answer shortcut is used by the live launcher.

## First Setup

Run from the repository root with Python 3.12 or newer:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ollama serve
# In another terminal:
ollama pull mannix/defog-llama3-sqlcoder-8b
gcloud auth application-default login
gcloud auth application-default set-quota-project gen-lang-client-0810987953
.venv/bin/python -m analytics_service.setup_local
```

The last command is read-only: it compares every live table, recursive schema and row count with the saved catalog/manifest, stopping on drift. It does not verify full row-content hashes.

After administrator approval, run `.venv/bin/python -m analytics_service.setup_local --apply`. This provisions twelve authorized views in `aml_analytics_demo`, a dedicated `aml-analytics-demo` service account, project BigQuery Job User, view-dataset READER, and an impersonation grant for the verified signed-in user on that account only. It may enable the IAM Credentials API. No billing configuration, source table replacement, or public deployment is performed. Existing IAM/access entries are preserved. Unexpected existing views stop setup rather than being overwritten.

This local demo permits all seven entity-country scopes. Contact fields (addresses, phone numbers, emails, birth dates and IP addresses) are excluded. Separately verify that the execution principal has no direct base-table access or write permissions. For country-restricted users, provision separate filtered views and a distinct service account per scope; changing the displayed scope list is insufficient.

Setup writes ignored, owner-only `analytics_service/config.local.json` and `analytics_service/access-token.local.json`. Enter the latter's `access_token` in the UI. Do not commit either file or send credentials in chat. Re-running setup refuses to overwrite local configuration. Partial cloud setup can be rerun after its error is resolved, provided no local configuration was written.

## Start Locally

```sh
.venv/bin/python -m analytics_service.local
```

Open [AML Analytics](http://127.0.0.1:8011/ui/). The launcher starts the model on loopback port 8000 and analytics on 8011; it refuses occupied ports. Ollama must already be running. Ctrl-C stops both Python services. Use `--port 8012` for another UI port.

| Variable | Launcher value |
| --- | --- |
| `MODEL_BACKEND` | `ollama` |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `mannix/defog-llama3-sqlcoder-8b` |
| `SCHEMA_BACKEND` | `catalog`: deployed schema JSON; no FalkorDB credentials required |
| `OLLAMA_NUM_CTX` | `8192` |
| `OLLAMA_TIMEOUT_SECONDS` | `240` |
| `AML_ANALYTICS_CONFIG` | Absolute local analytics configuration path |

Google authentication uses local ADC, then impersonates the configured read-only service account. There is no Gemini key or Google credential in the browser. The browser calls its same-origin analytics service with its local bearer token, held in memory until disconnect/reload.

## API and Boundaries

| Route | Contract |
| --- | --- |
| `GET /health` | Process liveness only |
| `GET /status` | Authenticated model and authorized-view readiness |
| `POST /query` | Authenticated `{"question":"..."}`; client SQL/scope fields rejected |
| `GET /metrics` | Optional reviewed questions; not a free-form mode restriction |
| `/ui/` | Static shell; no data without authentication |

The adapter adds backward-compatible `execution_mode` and `allowed_columns` fields to `/generate-sql`. Execution mode calls Ollama using native BigQuery STRUCT/ARRAY types and enums, without exemplar answers or fuzzy SQL repair. The original default model pathway remains available. The schema catalog supplies metadata, not customer examples. FalkorDB is optional for the legacy pathway, not required by this local integration.

A narrow AST normalization converts SQLCoder's known `DATE_TRUNC('month', timestamp)` argument order to GoogleSQL. It records corrections in the model response and never guesses identifiers or removes extra statements. Other invalid SQL is rejected; all normalized SQL still passes independent validation and a BigQuery dry run.

Candidates must pass one-read-only-statement, resource, function and schema checks. Table references are rewritten to authorized views. BigQuery performs a dry run before execution: configured 100 MB scan budget, 60-second execution deadline, best-effort cancellation, 1,000-row and approximately 2 MB response limits. Local inference has a configurable 240-second timeout with no automatic retry. These checks do **not** prove that arbitrary generated SQL matches business intent; review executed SQL for consequential analysis.

Responses carry typed columns, exact decimal strings, rows, executed SQL, request/job IDs, scan bytes, scope, data period, simulation warnings and deterministic charts. Tables preserve NUMERIC values; charts use approximate JavaScript numbers. Truncated results suppress charts. Ambiguous chart dimensions remain tables. No query results go back to the model. Empty results stay empty.

Party has historical versions; current-customer joins must deduplicate by latest `validity_start_time`. Risk-score periods must be explicit. Money uses normalized USD `units + nanos / 1e9`, not STRUCT-to-number casting. SAR events differ from distinct SAR cases. The prompt explains these rules; domain-owner review remains necessary.

## Tests and Limits

```sh
.venv/bin/pip install -r analytics_service/requirements-dev.txt
.venv/bin/python -m pytest analytics_service/tests -q
```

Unit tests use explicit doubles/DuckDB fixtures isolated from the application. Live verification must exercise actual Ollama and BigQuery, compare gold values and verify IAM denials. Skipped live tests do not prove connectivity. See `VERIFICATION.md` for evidence and limitations.

No cloud compute, GPU hosting or public endpoint is deployed. Before network deployment, replace local bearer provisioning with approved identity, add TLS/ingress controls and centralized audit/rate limiting, and review SQL/model risks. Logs exclude raw questions, SQL, credentials and results. This is not a banking compliance certification or automated AML decision system.
