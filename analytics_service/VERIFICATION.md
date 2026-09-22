# Analytics Verification

**Current status, 2026-09-22:** explicit GCP approval received and applied. The real local application runs at `http://127.0.0.1:8012/ui/`; it is connected to BigQuery through authorized views and the requested local Ollama model. No public deployment or billing activation was performed.

## Connected Verification

- Created twelve `aml_analytics_demo` views, excluding configured contact fields, and `aml-analytics-demo` service account. Granted project Job User, view-dataset READER, and signed-in-user Token Creator on that account only. Enabled required IAM, IAM Credentials and Cloud Resource Manager APIs. Source tables and rows were not replaced.
- Corrected ADC quota-project header to the verified numeric project ID `1076784773678`. OpenID identity checks omit a Cloud quota header. API/IAM propagation caused transient failures; final readiness reports both model and all twelve views ready.
- Using the actual execution identity: `testIamPermissions` on the Transaction view returned only `bigquery.tables.getData`, not update/updateData/delete. Direct base-table dry-run access was denied.
- Live question: "Show transaction count grouped by direction, with one row per direction." Result: CREDIT 13,035; DEBIT 36,965. Initial browser job `aml_70df9f76e0bd4e1cba4d6c5fa17a1863`; post-retrieval-change job `aml_c15df55159724b758ae7b4007245bb11` agreed.
- Live monthly distinct SAR case counts: February 8, March 8, April 8, May 7, June 14. Job `aml_2e00039b68c0455f85be1da4f0b1fb3b`. No rows were fabricated for months absent from the result.
- Independent reviewed SAR query matched all five monthly counts: job `aml_c4451afbbe724b968cb4e3423a6af645`.
- Latest targeted check returned 1,000 distinct customer IDs: job `aml_3d9cf642d2224476b60aada55173bbc8`. The unsafe active-customer aggregate now returns HTTP 422 `historical_ambiguity`, with no execution. A monthly monetary question still produced SQL that BigQuery rejected; it returns HTTP 422 `invalid_google_sql`, not fabricated results or a misleading connectivity error.
- Authenticated the actual Codex browser via the private local access file. Live chart/table inspected at desktop and mobile sizes with no horizontal overflow. Restored the user's browser size afterward. Browser automation could not expose canvas pixel buffers; visual chart verification was used, not an asserted pixel count.
- Unit/regression suite after fixes: 108 passed, six opt-in tests skipped in that run, two dependency deprecation warnings.

### Model Accuracy Remains Incomplete

The separate initial live six-KPI gold-comparison run had **six failures**: nested-field alias validation, a wrong-table SAR query rejected by BigQuery, three output-contract/meaning differences, and an active-customer answer of 0 versus gold 960. A later active-customer generation counted 1,100 historical rows instead of 960 latest active customers. Do not treat HTTP 200 or schema validity as proof of correct business meaning.

Subsequent fixes resolve nested fields before alias canonicalization, focus schema retrieval, normalize a narrowly recognized DATE_TRUNC dialect difference using ASTs, clarify lifecycle semantics, and reject ambiguous historical Party aggregates. Targeted live retests succeeded for direction counts and SAR cases. The full six-KPI accuracy suite has not been declared passing; keep PR #3 draft pending that acceptance work.

The earlier September 19 and pre-approval checkpoints below are retained as historical records, not current connectivity claims.

## Archived Results - 2026-09-19

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

The final refresh found the model owner's new main commit `a2ea455` (schema, graph, enum and retrieval updates). It was merged without conflicts into both review branches. The `/generate-sql` response contract remains compatible by source inspection, the model-owned paths match current `origin/main`, and the full 90-pass/6-skip suite was rerun after the merge. No live model behavior is inferred from this source check.

Before enabling live access, follow the service README to approve view filters and IAM, configure local identity/ADC, start the actual model endpoint, and run the six explicit live tests. Preserve failures for review instead of substituting fixture results. After the foundation merges, update the dependent PR's base to `main` and merge current `main` without force-pushing.
# Live Integration Checkpoint: 2026-09-22

Branch: `codex/ollama-bigquery-live`, created from remote `main` at `0878868`, then merged with the existing analytics branch. No direct changes were pushed to `main`.

## Verified This Session

- Google ADC login succeeded and uses quota project `gen-lang-client-0810987953`.
- Live read-only inspection matched all 12 deployed table schemas (including nested types/modes), region `asia-south1`, and manifest row counts. Transaction has 50,000 rows; Party has 1,140 historical rows for the documented 1,000 customers. Full content hashes were not recomputed.
- Ollama 0.34.2 and `mannix/defog-llama3-sqlcoder-8b` were installed locally. Actual generation returned a schema-valid credit/debit count query in about 22 seconds on the first request.
- Actual monthly-money generation exposed Postgres-style DATE_TRUNC ordering. Added a narrow AST normalization, with tests preserving statements and identifiers; no fuzzy identifier repair runs in execution mode.
- Actual India risk-score generation initially used an incorrect occupation predicate. Explicit country-name/prefix guidance corrected the predicate on retest. The retest still used an unnecessary historical Party join; min/max are invariant to those duplicate versions, but this is evidence that schema validity is not proof of business correctness.
- Unit/regression run: 104 passed, 6 live tests skipped, 2 existing dependency deprecation warnings. Test fixtures are not used by the live launcher.
- Disconnected UI checked in headless Chrome at 1440x1000 and 390x844: no horizontal overflow, question controls disabled before authentication. Screenshots were saved locally; live chart pixel/value checks remain pending.
- The old fixture preview on port 8011 was stopped to avoid presenting mock data as live results.

## Pending Access Approval

The security reviewer blocked provisioning persistent IAM/view changes until explicit approval. **No new authorized views, service account, impersonation grant, or live analytics configuration has been created by this setup.** Read-only dataset inspection is not an end-to-end connection test.

Approval is requested for twelve `aml_analytics_demo` authorized views, the `aml-analytics-demo` service account, project Job User plus read-only view access, and signed-in-user impersonation of that account only. Source rows, billing and public deployment are outside the setup.

After approval: run approved setup, verify denied direct base-table/write access, start the real analytics service, compare diverse NLP results against independent gold queries, run desktop/mobile live browser tests, and update this checkpoint with actual BigQuery job IDs. No claim of completed live dashboard integration is made yet.

---
