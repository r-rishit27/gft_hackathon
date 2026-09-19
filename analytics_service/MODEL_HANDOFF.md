# Model Team Handoff

The model owner retains `app.py`, `pipeline/`, `training/`, `eval/`, `graph/` and the existing frontend. This package must not import those internals. No Gemini integration or credential is implemented here.

## HTTP Contract

Analytics calls `POST /generate-sql` with `{"question":"..."}`. The URL is server-configured; HTTP is allowed only on loopback, otherwise HTTPS is required. There is no redirect following or automatic retry. The HTTP transport timeout is 30 seconds. Response size is capped at 256 KB; candidate SQL is capped at 32 KB.

Required response fields:

```json
{"sql":"SELECT ...", "schema_valid":true, "schema_violations":[]}
```

Additional fields remain compatible. Missing fields, string `"true"`, nonempty violations, non-2xx responses and timeouts fail closed. Model flags are advisory: analytics always parses, authorizes and validates the SQL independently.

## Schema and KPI Definitions

`model_contract.json` is generated from the deployed schema and the six starter definitions. Regenerate it with:

```sh
python -m analytics_service.handoff --output analytics_service/model_contract.json
```

It contains all recursive types, modes and declared enum values, but no observed values, row samples, IDs or data records. Only the schema is shared here; bank operators must approve the KPI meanings before real-data use. Our tests exercise these definitions; they do not establish banking-policy approval.

Use unqualified canonical table names or their full `gen-lang-client-0810987953.aml_demo` paths. Analytics resolves physical sources and replaces them with the authenticated user's approved views. Do not generate a view name, choose a service account or include authorization filters on behalf of the user.

V1 is deliberately limited to the canonical questions and aliases in the contract. Unsupported wording or extra filters yields `clarification_required`. The model receives a canonical question, never arbitrary user-supplied free text. Its candidate must match the starter SQL after AST qualification; whitespace and resource qualification differences are normalized, but algebraically equivalent rewrites are not guaranteed to pass. Do not treat this as unrestricted text-to-SQL. Expand the catalog and gold result tests together to support additional intents.

## Meaning of the Six KPIs

| Metric | Definition |
| --- | --- |
| Transaction trend | Non-deleted transactions by UTC booking month and direction. Amount = NUMERIC units + NUMERIC nanos / 1e9, normalized USD. Current synthetic Transaction IDs are unique; re-review if historical transaction versions are introduced. |
| SAR volume | Distinct cases with an AML_SAR event within each month; multiple filings for one case in the same month count once. A case can appear in multiple months. |
| Case backlog | Cases with a process-start event only. Compare latest start and end; a later start reopens a case. Alert-only cases are excluded. |
| Risk trend | Mean non-null simulated score per reporting period; count distinct customers with a non-null score in that period. |
| Active customers | Latest Party version strictly before September 1, 2026 UTC; not deleted, joined before cutoff, and no exit before cutoff. Never count historical Party rows as separate customers. |
| Alert conversion | Distinct alerted cases with any SAR in the available dataset divided by distinct alerted cases. This is an observed-period association, not a causal or fully matured cohort conversion. Zero denominator returns null. |

Common review points: dataset freshness is August 2026, not live September; case IDs and event IDs are different units; normalized USD is not a sum of mixed local currencies. The empty commercial-registration table is preserved, and omitted `civil_status_code` is not a valid deployed field.

## Integration Checklist

1. Update your graph/retrieval to the sanitized deployed schema; preserve original reference schema files.
2. Keep the response contract stable and return the reviewed SQL for supported intents. Do not claim SELECT-only training is a security control.
3. Start your service on port 8000. Run analytics independently on 8011.
4. Run the opt-in live test suite after scoped views, IAM and analytics configuration are ready. Unsupported model rewrites should fail, not execute a guessed replacement.
5. Make future response fields additive. Coordinate changed field names, supported questions and SQL shapes before merging model changes into `main`.
