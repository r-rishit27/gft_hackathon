# Governed AML Analytics Architecture

Design and repository assessment: 2026-09-19. This document proposes the next architecture; it does not deploy services or certify banking compliance.

Implementation update: `analytics_service/` now provides an isolated six-KPI prototype, independent validation, a BigQuery adapter and an offline-tested dashboard. See [`analytics_service` in Detail](#analytics_service-in-detail) below and [verification report](../analytics_service/VERIFICATION.md). The inventory below records the original model-service baseline, not the new package. Live integration and cloud IAM/perimeter verification remain pending.

Deployment update (2026-09-25): a PoC instance of `analytics_service` and the model service are now publicly reachable — see [Current Deployment](#current-deployment) below. This is a fast, minimal deployment for demonstration, not an implementation of the target design in this document; none of the Cloud Run, Secret Manager, VPC-SC perimeter, or structured audit-log elements described below are in place for it.

## Current Deployment

| Component | Where | Notes |
| --- | --- | --- |
| `analytics_service` (product UI, auth, BigQuery execution) | Render, `aml-analytics-service`, free tier | Public HTTPS; role-based login as designed in [`analytics_service` in Detail](#analytics_service-in-detail) below |
| `app.py` (model service, `/generate-sql` only) | Render, `aml-model-backend`, free tier | Public HTTPS but intended as an internal dependency only; called by `aml-analytics-service` over HTTPS, not loopback (the two run as separate Render services, each with its own memory, not one process pair sharing a container) |
| SQLCoder (`mannix/defog-llama3-sqlcoder-8b`) | Local machine, via Ollama, exposed through a Cloudflare quick tunnel | Not a cloud-hosted model endpoint; the tunnel is ephemeral and has no uptime guarantee. If the local machine or tunnel goes down, both Render services stay reachable but SQL generation stops working. |
| BigQuery access | Application Default Credentials from a personal `gcloud auth application-default login`, impersonating the three scoped service accounts (`aml-monitoring-poc`, `aml-investigation-poc`, `aml-admin-poc`) | Not a dedicated service-account key issued for this deployment; uploaded to Render as a secret file. Revocation requires redoing the login and re-uploading. |
| Role/session secrets (`config.roles.local.json`) | Uploaded to Render as a secret file, not committed to git | `profile-logins.local.json` (plaintext reference passwords) is never uploaded or read by the running server -- it exists only for a human operator to read the three demo logins from. |

This deployment does not implement §"Country and Row-Level Isolation"'s per-request entitlement architecture beyond what `analytics_service` already does locally (see below), has no Secret Manager, no VPC Service Controls perimeter, no Cloud Run isolation, and no structured/retained audit log beyond Render's own request logs. Treat it as a working demo of the six-KPI prototype, not a governed deployment satisfying the request-processing contract below.

### Component Diagram

```mermaid
flowchart TD
    Analyst((Analyst))

    subgraph AS[Analytics service]
        UI[Dashboard UI<br/>app.js]
        API[Analytics API<br/>app.py]
        AUTHN[Login and roles<br/>password_auth.py]
        MC[Model client<br/>model_client.py]
        POLICY[Query policy<br/>validator.py]
        BQX[BigQuery executor<br/>bigquery_client.py]
        DASH[Results dashboard<br/>dashboard.py]
    end

    subgraph US[User state]
        WS[(Saved questions and history<br/>workspace.py)]
    end

    subgraph SG[SQL generation]
        MAPI[SQL model API<br/>app.py]
        GEN[Generation pipeline]
        RETR[Schema retrieval]
        EXM[Exemplar matching]
        SQC[Ollama SQLCoder]
        REP[SQL repair]
        VAL[Schema validation]
        SEM[Semantic search<br/>semantic_search.py]
    end

    subgraph DATA[AML data]
        FDB[(FalkorDB graph)]
        META[(AML schema metadata)]
        BQ[(BigQuery dataset)]
    end

    Analyst -->|asks question| UI
    UI -->|submits query| API
    API -->|returns results| UI
    UI -->|shows results| Analyst
    UI -->|reads and updates| WS

    API -->|authenticates| AUTHN
    API -->|requests SQL| MC
    MC -->|returns SQL| API
    API -->|checks scope| POLICY
    API -->|executes approved SQL| BQX
    API -->|formats results| DASH

    MC -->|sends question| MAPI
    MAPI -->|dispatches generation| GEN
    GEN -->|retrieves context| RETR
    GEN -->|checks matches| EXM
    GEN -->|generates SQL| SQC
    GEN -->|repairs candidate| REP
    GEN -->|validates SQL| VAL

    RETR -->|scores similarity| SEM
    RETR -->|searches schema| FDB
    VAL -->|checks against| META
    BQX -->|queries| BQ
```

Two processes, one request path: `app.py` (top right, "SQL model API") is the same model service documented throughout this file and deployed as `aml-model-backend`; `analytics_service.app` (top left, "Analytics API") is deployed as `aml-analytics-service` and is the only component a browser talks to directly. `model_client.py` calls the model service exactly like any other HTTP client would — over `model_url`, never in-process — so the two can run as separate Render services (the deployed split) or as sibling processes on one machine (`analytics_service.local`, loopback only) without any code change.

### `analytics_service` in Detail

This is the deployed product (`aml-analytics-service`): a role-based, BigQuery-executing wrapper around the model service above, not just a thinner client for it. Consolidated from the package's own former README (now folded in here; see git history for the original file).

**Roles and scope.** Sign in at `/login` with `monitoring`, `investigation` or `admin` — passwords live in the gitignored, owner-only `analytics_service/profile-logins.local.json` (salted scrypt hashes are what's actually checked, stored in `config.roles.local.json`). The query page itself has no sign-in form; `/profile` shows the signed-in identity's permitted tables, countries and entity codes, with sign-out there.

| Profile | Permitted logical tables through authorized views |
| --- | --- |
| Monitoring | Party, AccountPartyLink, Transaction, InteractionEvent, PartySupplementaryData, RetailPartiesRegistration, CommercialPartiesRegistration |
| Investigation | Party, RiskCaseEvent, RiskScores, Explainability |
| Admin | Union of Monitoring and Investigation (10 distinct tables); no administrative write permissions |

All three profiles cover the same seven entity-country scopes as the rest of this document. Each role uses a distinct read-only BigQuery service account (`aml-monitoring-poc`, `aml-investigation-poc`, `aml-admin-poc`) with table-level Data Viewer grants on only its permitted authorized views, plus project Job User — none has direct source-table access, and `RegisteredPartiesExport`/`ExportedMetadata` are excluded for every role including Admin. The server selects scope and execution principal from the authenticated identity; a request cannot supply its own role or scope. The model only ever sees permitted schema, and the independent SQL validator rejects forbidden resources even if the model generates one anyway. A restricted request gets HTTP 403 ("You do not have the required access to answer this question"); a conservative intent check catches clear restricted-table asks, while ambiguous/negated mentions fall through to independent SQL validation instead of being blocked outright.

**Sessions, history, and the FAQ panel.** Sessions are opaque, HttpOnly, `SameSite=Strict`, expire after one hour, are revoked on logout/account switch, and reset on service restart — nothing is stored in browser storage. Unsafe same-origin requests need a custom header (no cross-origin API allowance at all); sign-in is rate-limited globally to 10/minute. History keeps the last 100 queries per identity+role (question, completion status, timestamp — never SQL or result rows) in a mode-0600 SQLite file; up to 50 saved questions persist alongside it. `/auth/login`, `/auth/logout`, `/auth/me`, `/workspace`, `/workspace/saved` and `/workspace/history` back this UI; `/query` only ever accepts a question, never client-supplied SQL/role/scope.

**Generation contract with the model service.** `analytics_service` extends the base `/generate-sql` contract with backward-compatible `execution_mode` and `allowed_columns` fields — execution mode has Ollama emit native BigQuery `STRUCT`/`ARRAY` types and enums directly, skipping exemplar answers and fuzzy SQL repair. Retrieval defaults to the same hosted FalkorDB graph and hybrid lexical/semantic search the base pipeline uses (`SCHEMA_BACKEND=falkordb`, with `catalog` as an explicit offline alternative); a graph failure stops generation rather than silently degrading to catalog mode. A narrow AST normalization step fixes SQLCoder's known `DATE_TRUNC('month', timestamp)` argument-order quirk against GoogleSQL, recording the correction rather than guessing identifiers or dropping statements; anything else invalid is rejected outright.

**Execution guardrails.** Every candidate must pass one-read-only-statement, resource, function and schema checks before its table references are rewritten to the caller's authorized views. BigQuery then dry-runs the exact final query under a 100MB scan budget and a 60-second execution deadline (best-effort cancellation), with 1,000-row/~2MB response limits on the real run; local inference itself has a 240-second timeout with no automatic retry. None of this proves the SQL matches business intent — it proves the SQL is safe to run against exactly the resources this identity is allowed to see. Domain-specific correctness rules (Party's historical `validity_start_time` versions needing dedup by latest, explicit risk-score periods, `units + nanos / 1e9` monetary math instead of STRUCT-to-number casts, SAR events vs. distinct SAR cases) are enforced by prompt instruction plus a conservative rejection of un-snapshotted Party aggregates, not by the database schema alone.

**API surface:**

| Route | Contract |
| --- | --- |
| `GET /health` | Process liveness only |
| `GET /status` | Authenticated model and authorized-view readiness |
| `POST /query` | Authenticated `{"question":"..."}`; client-supplied SQL/scope fields are rejected |
| `GET /metrics` | Optional reviewed questions; not a free-form-mode restriction |
| `/login` | Dedicated username/password login; active sessions redirect to analysis |
| `/profile` | Authenticated profile with server-derived permitted tables and countries |
| `/ui/` | Analysis page; signed-out visitors redirect to login |

**Local setup** (Python 3.12+, from the repository root):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ollama serve
# In another terminal:
ollama pull mannix/defog-llama3-sqlcoder-8b
gcloud auth application-default login
gcloud auth application-default set-quota-project 1076784773678
.venv/bin/python -m analytics_service.setup_local
```

`setup_local` (no `--apply`) is read-only: it diffs every live table, recursive schema and row count against the saved catalog/manifest and stops on drift. After administrator approval, `setup_local --apply` provisions the twelve authorized views, a dedicated `aml-analytics-demo` service account, project Job User, view-dataset READER, and an impersonation grant scoped to the verified signed-in user only — it enables IAM/IAM-Credentials/Cloud-Resource-Manager APIs as needed but performs no billing changes, no source-table replacement, and preserves any existing IAM/view entries (an unexpected existing view stops setup rather than being overwritten). `1076784773678` is this project's verified numeric ID; the text project ID produced misleading API-disabled errors as the ADC quota-project header during setup. Role setup (`setup_roles`, `--apply`, `--verify-only`) and admin setup (`setup_admin --apply`) follow the same inspect-then-apply pattern and never overwrite existing local passwords or credentials.

**Running locally:** `python -m analytics_service.local --port 8012` starts the model service on loopback `:8000` and `analytics_service.app` on the given port together (refusing already-occupied ports), printing the login URL; Ctrl-C stops both. This is the same two-process pair as the deployed Render split, just sharing one machine instead of two — see [Current Deployment](#current-deployment) above for why the deployed version runs them as fully separate services instead.

**Tests:**

```sh
.venv/bin/pip install -r analytics_service/requirements-dev.txt
.venv/bin/python -m pytest analytics_service/tests -q
```

Unit tests use explicit doubles/DuckDB fixtures, isolated from the live application. A live browser check (`node analytics_service/tests/ui_roles.cjs`, Playwright, `NODE_PATH`, local Chrome, the service running) exercises all three role profiles against real BigQuery-backed questions; `ui_smoke.cjs` similarly drives one real query as Monitoring plus isolated pagination/empty/partial-result rendering cases. Skipped live tests prove nothing about connectivity — see `analytics_service/VERIFICATION.md` for the actual evidence and its limitations. None of this is a banking compliance certification or an automated AML decision system.

## Scope and Current Boundary

- Target: `gen-lang-client-0810987953.aml_demo`, documented dataset location `asia-south1`.
- Data is synthetic. Risk scores, explainability and model metadata are simulated, not actual AML AI model outputs.
- Seven entity-country scopes: `HASE_HK`, `HSBC_GB`, `HSBC_IN`, `HSBC_TW`, `HSBC_FR`, `HSBC_PL`, `HSBC_IE`.
- The reference image calls for governed self-service analytics, authorization and traceable results. BigQuery is the first data source; NoSQL expansion is not part of this phase.
- A project display name such as "Gemini API" does not establish a VPC Service Controls perimeter. Live IAM, billing, perimeter configuration and model endpoint residency were not verified during this architecture review.
- Repository and local dataset artifacts were inspected. Cloud deployment status must be checked separately before implementation.

## Target Diagram

All application components below are proposed unless identified as existing in the implementation inventory. The project box is an ownership boundary, NOT a claim of a configured security perimeter.

```mermaid
flowchart TD
    U[Authenticated business user] --> UI[Chat UI: question, results and charts]
    UI --> AUTH[HTTPS identity verification and rate limits]
    subgraph P[Target GCP project: gen-lang-client-0810987953]
        AUTH --> API[FastAPI orchestrator: proposed Cloud Run deployment]
        API --> POLICY[Resolve trusted entity-country entitlements; redact input]
        POLICY --> CAT[Sanitized schema and approved KPI catalog]
        CAT -. Optional metadata retrieval .-> KG[FalkorDB schema graph]
        CAT --> ADAPTER[Server-side Gemini adapter]
        SM[Secret Manager: demo Gemini API key] -.-> ADAPTER
        V[Fail-closed SQL AST, schema and authorization validator]
        V --> SEM[Metric semantics and ambiguity checks]
        SEM --> DRY[BigQuery dry run: same final SQL and parameters]
        DRY --> EXEC[Bounded executor: scoped IAM principal, bytes and time limits]
        EXEC --> BQ[(Approved BigQuery views over aml_demo)]
        BQ --> GUARD[Result policy: masking, size limits and permitted detail]
        GUARD --> DASH[Deterministic insight and chart-spec builder]
        DASH --> RESP[Typed results, charts, SQL provenance and freshness]
        API -.-> AUDIT[Redacted audit events and monitoring]
        V -.-> AUDIT
        EXEC -.-> AUDIT
        GUARD -.-> AUDIT
    end
    ADAPTER --> LLM[Gemini API: separate service boundary]
    LLM -->|Untrusted candidate SQL; no DB credentials| V
    V -->|Rejected| CLARIFY[Reject or ask a clarification; do not execute]
    SEM -->|Ambiguous| CLARIFY
    DRY -->|Error or budget exceeded| CLARIFY
    CLARIFY --> UI
    RESP --> UI
```

The API key authenticates the backend's model call only. BigQuery uses a separate IAM identity. No API key, service-account credential or raw model prompt is exposed to the browser. Results do not return to Gemini by default.

### Model Access Choices

**Synthetic demo:** server-side Gemini Developer API key, stored outside Git, preferably Secret Manager for a deployed service. Send only sanitized questions, approved schema metadata, permissible enum definitions and business rules. Disable unnecessary external grounding and tools. Do not send the full enriched dataset JSON: its examples and observed values can contain row-level information.

**Real banking data:** select an enterprise Google Cloud Gemini deployment with IAM-based workload authentication, approved endpoint/model/location and verified organization controls. Prefer an attached service account with Application Default Credentials over downloadable key files. Verify model-specific VPC Service Controls and residency support before drawing an enforced perimeter. A paid API key alone is not a data-residency guarantee. Google documents supported controls by model in its [security controls matrix](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/security-controls).

Google's Gemini Developer API terms distinguish unpaid and paid processing. Unpaid service terms generally permit product improvement and human review and prohibit submitting sensitive information; there are EEA/UK/Switzerland exceptions. Paid processing has different protections but still describes limited abuse-monitoring retention and possible processing locations. Review the applicable terms before introducing confidential data. [Gemini API terms](https://ai.google.dev/gemini-api/terms)

## Request Processing Contract

1. Authenticate the user and resolve permissions on the server. Country names or prefixes in a prompt are not authorization. Reject excessive input and redact confidential identifiers before model access.
2. Retrieve metadata only for approved resources. Maintain one versioned catalog from `dataset/aml_data_model_schema.json`, recursively retaining nested fields, types and modes. Distinguish contractual `allowed_enum_values` from merely `observed_values`.
3. Request structured model output containing a candidate GoogleSQL query and typed parameters, or a clarification. Do not let the model submit jobs or choose execution credentials. Exemplar-based queries follow the same validation path.
4. Parse the entire query using a maintained BigQuery-dialect AST parser. Permit exactly one read-only query, including supported CTEs. Resolve all referenced resources and nested fields. Allowlist projects, datasets, views, columns and functions. Reject scripts, DDL, DML, `EXPORT DATA`, `CALL`, dynamic SQL, external queries, remote functions and unapproved routines. Parameterize user values; validate identifiers separately. Regex and a SELECT-only prompt are insufficient.
5. Validate business meaning and ambiguity. Read-only SQL can still disclose data or compute the wrong KPI. Do not silently fuzzy-repair an unknown banking metric into a different one. Any repair must pass every gate again; bound retries.
6. Dry-run the exact final SQL with its parameters, region and execution identity. Apply estimated-byte limits, then enforce `maximum_bytes_billed`, execution deadline, cancellation, concurrency limits and output pagination on the actual job. A dry run is not an authorization policy or an absolute cost guarantee; `LIMIT` alone does not limit bytes scanned. [BigQuery query execution](https://docs.cloud.google.com/bigquery/docs/running-queries)
7. Execute with least privilege: query job permission in the execution project and read access only to approved resources. No application Data Editor/Owner role. Provisioning/migration scripts and their write-capable credentials stay outside this request path.
8. Apply result disclosure rules, then produce typed tables and deterministic chart specifications. Return request ID, executed SQL or policy-approved representation, parameters with sensitive values redacted, job ID, data-as-of date, applied scope, units, truncation and synthetic-data status.
9. Record redacted operational audit events: identity, entitlement scope, schema/model version, SQL hash, validation decision, BigQuery job ID, bytes, latency and failures. Do not log API keys, full sensitive prompts or raw result rows. Restrict audit access and define retention.

### Country and Row-Level Isolation

BigQuery evaluates the execution principal, not the browser user's claimed identity. One broadly privileged service account does not automatically provide different user entitlements. For the first governed version, map trusted user entitlements to approved scope-specific views and execution principals with no base-table read access. Alternatively, design and verify delegated end-user identities with row policies. Multi-country users receive only their explicitly approved union of scopes. Apply column restrictions to PII and SAR-related detail as well as row filters. [BigQuery row-level security](https://docs.cloud.google.com/bigquery/docs/row-level-security-intro)

Cache keys must include entitlement scope, query parameters and schema version. Check ownership on result retrieval, cancellation and exports; an unguessable job ID is not authorization. Review small-group disclosure and export permissions under bank policy. Synthetic multi-country data in Mumbai does not establish approval to centralize real banking records there.

## Dashboard Builder

Implement a trusted Python module that converts typed results and approved KPI definitions into a constrained declarative chart specification. The browser renders the specification with a chart library; neither Python nor JavaScript produced by the LLM is executed.

| Result shape | Output |
| --- | --- |
| One approved scalar metric | KPI value, units and period |
| Time dimension plus numeric metric | Time-sorted line chart with explicit timezone |
| Category plus numeric metric | Bar chart with bounded categories and an explicit remainder policy |
| Multiple dimensions or detailed records | Paginated table; chart only when semantics are known |
| Empty, partial, suppressed or invalid result | Explicit state; no invented insight |

Insight text is calculated from actual result values: totals, changes and ranked categories with stated denominators. Avoid causal claims. Respect currency, decimal precision, nulls and missing time buckets. A chart must never silently represent a truncated result as the complete dataset. CSV/Excel exports, when added, require the same authorization and safe spreadsheet-cell handling.

Banking-specific metric definitions must establish Party historical-record joins, latest-versus-as-of risk scores, distinct cases versus case events, alert/SAR denominators and `units + nanos / 1e9` monetary interpretation. Do not mix normalized USD and local-currency companion amounts. "Latest" means latest available dataset period, not necessarily today's live position.

## Original Repository Baseline

| Component | Verified code/artifact | Gap to target |
| --- | --- | --- |
| Chat UI and HTTP API | `frontend/index.html`; `app.py` provides `/generate-sql`, `/health`, `/ui` | SQL preview only; no executed results or analytical charts |
| Schema retrieval | `pipeline/text2sql_falkordb.py`; `graph/build_falkordb_graph.py` | Graph builder still points to original `schema/aml_data_model_schema.json`; refresh from sanitized deployed schema |
| SQL generation | Local T5 with fine-tuned-directory fallback and exemplar retrieval | No Gemini client or server-side API-key integration |
| Validation and repair | `pipeline/schema_validator.py`: regex, fuzzy table/column correction, enum hints | Not a structural read-only, authorization or cost gate; `schema_valid` is not proof of safe execution |
| Validation response | `app.py` returns `schema_valid` and violations | Flags are returned to the caller, not a fail-closed execution decision |
| Schema documentation | `dataset/aml_data_model_schema.json`: deployed schema and value metadata | Runtime validator uses old schema and one-level `subfields`, not recursive deployed `fields` and explicit enum metadata |
| Dataset tooling | `dataset/` contains generator, loading/migration and validation artifacts | Provisioning is not a request-time BigQuery executor; do not reuse write-capable provisioning credentials |
| Access control | No user authentication/authorization in inspected API; wildcard CORS | Add identity, entitlement enforcement, least-privilege execution, restricted origins and rate limits |
| Operational safety | `/health` checks model presence; exceptions expose their text | Add dependency readiness, redacted errors, job budgets, cancellation and audit events |
| Evaluation | `eval/`, `training/`, `docs/TEST_REPORT.md` | Existing evaluations are not proof of intent accuracy, security or query-result correctness |

This is a source inspection, not a fresh application test run or cloud security audit. No production controls are inferred from documentation alone.

## Implementation Sequence

1. **Unify the schema and metrics.** Build the sanitized recursive catalog from the deployed JSON, refresh graph metadata, define KPI semantics and versioned GoogleSQL fixtures. Preserve original schema reference files.
2. **Build the security gate before execution.** Add identity and scope resolution, AST validation, function/resource allowlists, semantic checks, scoped BigQuery access and adversarial tests. Never rely on a model prompt to grant access.
3. **Add Gemini behind a provider interface.** Keep local T5 as an optional baseline. Add bounded requests, structured responses, secret handling and redacted telemetry. Compare held-out intent and result accuracy, not just exemplar matches.
4. **Implement the BigQuery execution adapter.** Dry runs, explicit `asia-south1`, parameter binding, bytes/deadline limits, cancellation and typed result pagination. Test a permitted aggregate end to end before enabling more query shapes.
5. **Add dashboard and API results.** Suggested contract: `POST /query` accepts a question and permitted filters; returns results or `202` with request ID. `GET /queries/{id}` and `GET /queries/{id}/results` enforce ownership; `POST /queries/{id}/cancel` cancels an owned job. Keep `/generate-sql` as a protected preview endpoint. Internally build charts from results, never arbitrary executable model code.
6. **Release only after checks pass.** Test prompt injection, unauthorized countries/columns, external/remote calls, multiple statements, nested SQL and CTE bypasses, high-cost scans, model timeouts, cross-user result access and cache leakage. Verify numerical results against gold queries, historical joins and currency handling. Record model/schema versions and regression results.

Deployment to Cloud Run, Secret Manager, enterprise model endpoints or additional storage may require billing and organization approval beyond BigQuery Sandbox. Select and approve those services separately; this design does not enable billing or provision infrastructure. Before real banking data, complete privacy, residency, retention, security and model-risk reviews, and verify deployed IAM and perimeter policies with negative-access tests.
