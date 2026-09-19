# Independent AML Analytics Service

This service owns validation, authorized BigQuery execution and a separate dashboard. It uses the model team's existing HTTP endpoint and changes none of their source files. Start from the repository root with Python 3.12 or newer.

## Local Offline Preview

```sh
python3 -m venv analytics_service/.venv
analytics_service/.venv/bin/pip install -r analytics_service/requirements-dev.txt
export AML_DEMO_TOKEN="$(openssl rand -hex 24)"
analytics_service/.venv/bin/uvicorn analytics_service.demo:create_demo_app --factory --host 127.0.0.1 --port 8011 --no-access-log
```

Open http://127.0.0.1:8011/ui/ and enter the token you generated. The browser holds it only in memory. Choose a KPI and run the query. The offline fixture uses a tiny DuckDB dataset and a deterministic model stub; every response and the UI identify it as an offline fixture. It does not reproduce the live dataset's totals and never calls Google or the model service.

## Live Setup: Required Before Real Integration

1. Have an administrator create approved views in `asia-south1` with exactly the required country/entity filtering and approved projected columns. The example view names are placeholders, not existing provisioned resources. Use `party_id` prefixes for Party/RiskScores/RiskCaseEvent and owner `account_id` prefixes for Transaction. Match complete prefixes such as `HASE_HK_`, not a substring. Multi-country scopes need explicitly approved union views.
2. Authorize only these views on the base dataset. Give each scope's service account query-job permission in the project and read access only to its approved view resources. It must have no read access to the base dataset, other scopes, and no ability to create or edit views. Do not give the runtime Owner, Editor or Data Editor.
3. Use ADC from an attached workload identity (or approved local ADC for testing). The source identity needs impersonation rights only on the allowed scoped service accounts. Each scope has a distinct target principal. No service-account JSON key, loader token or `gcloud print-access-token` is used by this app.
4. Copy `config.example.json` to ignored `analytics_service/config.local.json`, choose an explicit scan budget, and replace the zero token hash with SHA-256 of a cryptographically random bearer token of at least 24 characters. Assign one token and scope per analyst. Keep the token secret and restart the service to revoke or rotate configured tokens. This is local integration authentication, not enterprise SSO.
5. Set `AML_ANALYTICS_CONFIG` to that file and start the model service independently. Start analytics with the command below. Verify country isolation and denied direct base-table access using each actual execution principal before exposing the service.

```sh
export AML_ANALYTICS_CONFIG="$PWD/analytics_service/config.local.json"
analytics_service/.venv/bin/uvicorn analytics_service.app:create_app --factory --host 127.0.0.1 --port 8011 --no-access-log
```

The app verifies configured resources are views in the expected region. It cannot infer whether an administrator wrote their filters correctly or granted extra IAM permissions. Those must be reviewed and tested separately. HASE/HSBC labels are not themselves an authorization mechanism. No views, service accounts, billing changes or cloud infrastructure are created automatically.

## API

| Route | Contract |
| --- | --- |
| `GET /health` | Public process liveness only; does not assert downstream readiness. |
| `GET /metrics` | Bearer-authenticated questions, units, assigned scope and schema version. |
| `POST /query` | Bearer-authenticated `{"question":"..."}`. Extra fields, including client-selected scope or SQL, are rejected. |
| `/ui/` | Public static shell; all data requests require authentication. |

Responses contain typed columns, rows, executed SQL, request/job IDs, scope, units, data-as-of, warnings, mode and deterministic chart specs. NUMERIC decimals and integers beyond JavaScript's safe range are serialized as strings; table text preserves them, while charts use approximate floating-point display. Non-finite floats become null. All data responses use `Cache-Control: no-store`.

V1 supports six fixed, reviewed SQL shapes, not arbitrary banking questions. Unsupported questions request clarification. Model candidates are structurally qualified and compared with the expected metric before execution. User values are never interpolated into SQL: v1 has no user-controlled query parameters. Add typed BigQuery parameter binding before enabling custom date/risk filters. Read [MODEL_HANDOFF.md](MODEL_HANDOFF.md) for exact scope and semantics.

Per request: 30-second model HTTP timeout; 60-second BigQuery operation budget; mandatory estimated/actual scan limits; 1,000 rows and approximately 2 MB of row JSON; bounded concurrency and per-identity rate limiting. BigQuery cancellation is best effort. Unconfirmed cancellation is reported rather than claimed successful. The deterministic job ID is `aml_` plus the response request ID without hyphens. An operator can use it to investigate uncertain submissions.

Charts/insights are suppressed on truncated results. No model-generated code executes, no result is returned to the model, no cross-user cache exists, and no export or background-job endpoint is exposed. The model's `schema_valid` flag is never sufficient to execute SQL.

## Verification

```sh
analytics_service/.venv/bin/python -m pytest analytics_service/tests -q
python -m analytics_service.handoff --output analytics_service/model_contract.json
```

Numerical fixtures run transpiled gold queries through DuckDB; they do not replace BigQuery dialect verification. The live tests are skipped unless explicitly enabled:

```sh
export AML_LIVE_TEST_URL=http://127.0.0.1:8011
export AML_LIVE_TEST_TOKEN="your analyst token"
AML_RUN_LIVE=1 analytics_service/.venv/bin/python -m pytest analytics_service/tests/test_live.py -q
```

Enabling this test runs six read-only BigQuery queries under configured limits and may consume quota/cost. It requires the actual model service, approved views and IAM configuration. Run it only after those prerequisites have been checked.

`tests/browser.cjs` performs desktop/mobile Playwright checks on the offline preview, including nonblank canvas pixels, query changes, authentication and overflow. It requires a separately installed `playwright` Node package and Chromium, and writes screenshots under `/tmp` by default.

## Operational Limits

- Bind to loopback for local work. Before network deployment: replace local bearer provisioning with approved enterprise identity, enforce HTTPS, review CSRF/origin controls for the chosen identity mechanism, restrict ingress, and configure central audit logging and distributed limits. The current in-memory limiter is per process; run one worker locally.
- Audit events contain subject, scope, SQL hash, schema version, job ID, bytes and latency, never raw questions, SQL, tokens or results. Configure the `aml.analytics.audit` logger at INFO in a protected log sink. Error details are redacted. The model endpoint currently does not provide a required model version; add that as a backward-compatible field before production traceability claims.
- All data and model outputs remain synthetic. This package is not a compliance certification and is not approved for automated AML decisions or real customer information.
- Chart.js 4.4.8 is vendored with its license, so charts make no third-party runtime requests. Google client, FastAPI and SQLGlot versions are pinned; review dependency upgrades with regression tests.
