# Analytics Verification - 2026-09-19

## Local Results

- Combined Python suite: **90 passed, 6 skipped**. Includes 76 analytics tests and the 14 existing dataset tests.
- The six skips are opt-in live model-to-BigQuery tests, not passing integration tests.
- Two upstream test-client deprecation warnings remain (Starlette/httpx and anyio); no test failed.
- GoogleSQL is parsed and qualified with pinned SQLGlot. Numerical gold fixtures execute through DuckDB transpilation. This is not a substitute for live BigQuery dialect/IAM testing.
- Desktop browser: 1440 x 1050. Two 675 x 280 charts with 12,194 and 12,076 nontransparent pixels.
- Mobile browser: 390 x 844. Two 354 x 250 charts with 8,232 and 8,107 nontransparent pixels.
- Both browser runs: no page JavaScript errors, no horizontal page overflow, query switching and KPI rendering passed, rejected queries cleared old results, disconnect disabled querying, and no credentials were persisted to local/session storage.

## Covered Behaviors

- Model compatibility, missing/invalid fields, extra fields, timeout, malformed responses and oversize responses.
- Read-only AST checks, CTE-contained forbidden resources, external/remote functions, scripts, writes, historical table decorators, unknown columns and nested fields.
- Semantic mismatches, changed filter literals and unsupported user intent fail before execution.
- Bearer identity, server-selected country scopes, distinct execution principals/view routing, rate limits, request limits and redacted audit output.
- Identical final SQL across dry run and execution, enforced scan budget, explicit region, deadline, cancellation and uncertain submission handling.
- Row/byte caps, decimal and large-integer serialization, null handling, empty and partial-result chart suppression.
- Distinct SAR cases, reopened investigations, latest historical Party selection, nested units/nanos monetary sums, non-null risk-score means/counts and distinct alert-to-SAR denominators.

## Not Yet Verified Live

- No service responded at `http://127.0.0.1:8000/health` during this run.
- No local ADC file was found at the standard gcloud location; gcloud is not installed here. No user credential file was opened or committed.
- Approved scope-specific views and IAM principals have not been provisioned or verified by this implementation. Example configuration names do not assert their existence.
- No cloud query, view creation, dataset replacement, IAM grant, billing change or model change was performed in this iteration.
- Enterprise identity, regional endpoint/perimeter controls, distributed rate limits, bank-policy approval and production deployment remain outside this local synthetic-data release.

## Review and Rollout

The foundation remains on `codex/aml-dataset-integration`. The dependent service/dashboard is on `codex/aml-analytics-service`; review it against the foundation branch first. Model-owned files are unchanged. Both branches must be integrated through review; nothing is pushed directly to `main`.

Before enabling live access, follow the service README to approve view filters and IAM, configure local identity/ADC, start the actual model endpoint, and run the six explicit live tests. Preserve failures for review instead of substituting fixture results. After the foundation merges, update the dependent PR's base to `main` and merge current `main` without force-pushing.
