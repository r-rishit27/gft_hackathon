# Analytics Verification

## Admin, Login and Access Messages - 2026-09-25

This checkpoint supersedes the two-profile login flow below. Changes remain local on `codex/ollama-bigquery-live`; nothing was pushed to `main`.

- After explicit approval, created `aml-admin-poc` with project BigQuery Job User, table-level read-only access to the exact 10-view union of Monitoring and Investigation, and signed-in-user impersonation on that new account only. Source data, billing and public deployment were unchanged.
- Verified Admin using actual impersonated BigQuery credentials: all 10 permitted views readable; RegisteredPartiesExport and ExportedMetadata denied; direct source Party denied; every allowed view exposes getData but not updateData, update or delete. Initial token-grant propagation delay resolved before restarting the app. Report: ignored `admin-access-verification.local.json`.
- Existing Monitoring/Investigation login records compare exactly with the private pre-Admin backup. Admin username is `admin`; the generated password is in the ignored `profile-logins.local.json`. Runtime config, login file and report are mode 0600. Active runtime has three password identities; Admin is a read-only analytics role, not a Google Cloud administrator.
- `/login` is now a separate page. Signed-out visits to `/ui/` or `/profile` redirect there. Authenticated `/login` visits redirect to analysis. No sign-in form remains on the query page. `/profile` displays server-derived tables, all seven authorized countries and entity codes; sign-out revokes the session.
- HTTP 403 access denials show "You do not have the required access to answer this question." Clear forbidden-topic requests fail before inference; physical forbidden table references in rejected model output also receive that message. Unrelated model failures retain their original error category. Negated or ambiguous mentions remain subject to SQL validation; the intent check never grants access.
- Regression suite: **140 passed, 6 opt-in live tests skipped**, one existing FastAPI/Starlette dependency deprecation warning. Tests cover page routing, country/table metadata, Admin union, early denial, CTE-shadowing/string-literal false positives, unrelated model errors and malicious generated SQL with no executor call.
- Actual Chrome role journeys passed for all three accounts: separate login, correct table counts (7/4/10), seven country rows, explicit forbidden-query messages, private FAQ/history, reuse, role-spoof rejection and logout. Profile and analysis pages checked at desktop 1440x1000 and mobile 390x844 with no horizontal overflow or JavaScript errors. Screenshots are under `/private/tmp/aml-profile-<role>-<viewport>.png` and `/private/tmp/aml-<role>-<viewport>.png`.
- Live graph/Ollama/BigQuery jobs: Monitoring transaction directions `aml_3761b53bc9d94d939b26423994d023d8` (2 rows); Investigation case event types `aml_36d6b758a60a4604a5880971ed7b14b3` (11 rows); Admin transaction directions `aml_c008f6c997a342f281765201eb92fe5a` (2 rows) and case event types `aml_9d6d2b893ae9462696ff2777e0c89131` (11 rows). No runtime fixture fallback used.
- Final dashboard browser regression passed: live job `aml_21b9703e4cce49c9893bf2e0dd92f521` returned CREDIT 13,035 and DEBIT 36,965. Verified chart pixels, exact table values, desktop/mobile overflow, collapsed technical details and sign-out. Isolated browser-rendering cases additionally tested empty/partial results, pagination and decimal precision; these are test-only inputs, never runtime results.

## Password Profiles and Private Questions - 2026-09-25

- Created approved `aml-monitoring-poc` and `aml-investigation-poc` service accounts with project Job User, table-level Data Viewer only on their respective existing authorized views, and signed-in-user Token Creator on those accounts. No source data, source schemas, billing or public deployment changed. Initial propagation delays were resolved before activating the profile configuration.
- Monitoring allowed: Party, AccountPartyLink, Transaction, InteractionEvent, PartySupplementaryData, RetailPartiesRegistration, CommercialPartiesRegistration. RiskCaseEvent, RiskScores, Explainability and both export/metadata views denied.
- Investigation allowed: Party, RiskCaseEvent, RiskScores, Explainability. Transaction, AccountPartyLink, InteractionEvent, supplementary/registration tables and both export/metadata views denied.
- Read-only BigQuery dry runs checked all 12 views under each actual role principal. Both principals were denied direct source Party access. `testIamPermissions` on every permitted view returned getData but no updateData, update or delete permissions. This verifies real IAM boundaries in addition to application validation.
- Active service uses `config.roles.local.json`, with only two password identities; the old broad demo bearer token is not present. Passwords generated locally, stored in owner-only ignored profile login file, salted scrypt hashes in runtime configuration. No passwords or session tokens printed in logs or committed.
- Live browser journeys: Monitoring returned two direction rows, job `aml_24cbe6252fe4480d95e3d9e14c84f758`; Investigation returned eleven event-type rows, job `aml_5a3fa12b198a496485fcd41a5a1c19ba`. Both used the real graph/model/BigQuery path, not fixtures. Saved FAQ, private history, question reuse, rejected request-supplied scopes and logout checked. Desktop/mobile screenshots inspected; no page overflow or JavaScript errors. Long event labels now wrap inside charts.
- Combined Python suite: 119 passed, 6 opt-in live tests skipped. New tests cover password login errors, HttpOnly/SameSite cookies, CSRF protection, forged roles, old-token rejection, forced forbidden model SQL with no executor call, separate principals, private history/FAQ, frequency counts, history clearing, session rotation/expiry and sign-in throttling.
- Local PoC only: one-hour in-memory sessions reset at restart, global sign-in throttling, loopback HTTP and a local private SQLite question store. These are not a production banking identity, perimeter or retention certification.

## Stakeholder UI - 2026-09-25

- Redesigned the existing UI without changing authentication, model retrieval, SQL validation or BigQuery access. Hidden routine infrastructure details and retained them in expandable Analysis details; critical warnings and synthetic-data disclosures remain visible.
- Combined Python regression suite: 116 passed, 6 opt-in live tests skipped. Added presentation tests for numeric identifiers, unrepresented dimensions, time/category charts, scalar zero and partial results.
- Separate real Playwright flow authenticated with the local access file, submitted the direction-count question and verified CREDIT 13,035 / DEBIT 36,965 from BigQuery job `aml_534a81cd337b4717aabea297eee2c5f7`. Desktop 1440px and mobile 390px screenshots inspected; chart-colored pixel checks and page overflow assertions passed after synchronizing responsive chart resizing.
- Browser checks also covered tab switching, hidden technical details, exact decimal formatting, 25-row pagination, empty results, partial warnings and sign-out. Edge-case inputs were isolated browser tests, never runtime fixture data. No browser JavaScript errors were recorded.

## FalkorDB Integration - 2026-09-25

- Fetched and reviewed `origin/main` at `0878868`; reused its hybrid lexical/semantic table retrieval and one-hop REFERENCES/LINKS_TO expansion on `codex/ollama-bigquery-live`. No main-branch or hosted-graph changes.
- Read the existing `aml_data_model` graph: 12 tables and 9 outgoing REFERENCES relationships. Retrieval uses graph descriptions and relationships with deployed BigQuery types/enums and approved-column restrictions. Unknown graph fields cannot expand the SQL authorization boundary. Graph failure stops generation; no silent catalog fallback.
- Stored supplied credentials only in ignored owner-only `.env`. Restarted the local launcher from persistent `.venv`. The old temporary environment lacked `pyvenv.cfg`, so an initial dependency install inadvertently updated system Python packages; the app now uses the isolated project environment.
- Model health reports `schema_backend=falkordb`, `backend=ollama`, and `ollama_ready=true`. Authenticated analytics status reports BigQuery ready with 12 authorized views. Health's schema backend is configuration metadata, not a continuous graph connectivity probe; the live retrieval/query below verifies connectivity for this run.
- Live authenticated question: "Show transaction count grouped by direction, with one row per direction." HTTP 200; CREDIT 13,035 and DEBIT 36,965, agreeing with the previous reviewed totals. BigQuery job `aml_a7cc9d6716e04e6ca730357572e481b7`, 1,213,035 processed bytes, no truncation. Response contained a deterministic bar-chart specification and synthetic-data warnings. No fixture rows or exemplar SQL were used.
- Combined analytics and dataset regression suite: 112 passed, 6 opt-in live tests skipped (analytics alone: 98 passed). Includes graph selection, field restrictions, STRUCT preservation, empty-graph failure, graph-outage failure and unauthorized-neighbor exclusion. One dependency deprecation warning remains. This single live smoke test does not replace the still-incomplete six-KPI accuracy evaluation below.

## Previous Checkpoint

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
