# Synthetic AML Dataset

Target: `gen-lang-client-0810987953.aml_demo`, location `asia-south1`.
Scope: 1,000 retail customers; exactly 50,000 transactions; January-August 2026.
Countries: Hong Kong (HK), United Kingdom (GB), India (IN), Taiwan (TW), France (FR), Poland (PL), Ireland (IE).
All people, accounts, events and outputs are fictional. Nothing here is a real SAR or real risk assessment.

## Entity-country identifiers (v2)

Hong Kong uses HASE_HK. Other country prefixes are HSBC_GB, HSBC_IN, HSBC_TW,
HSBC_FR, HSBC_PL and HSBC_IE. GB is the UK country code.
Identifiers retain their original numerical suffixes, for example HASE_HK_P0001,
HSBC_IN_A0003 and HSBC_FR_T000125. Customer names use HASE_HK_CUSTOMER_0001.
Source labels use HASE_HK_CORE, with equivalent prefixes for the other countries.
Transaction identifiers follow their account owner's country; cases and events
follow their customer's country. External counterparties use names such as
External HK Counterparty 0001; these labels do not imply bank affiliation.
Dataset-wide SIMULATED metadata resource identifiers are intentionally retained.
All values besides these identifiers and display/source labels are unchanged.

The updated `aml_data_model_schema.json` retains the supplied input/output sections
and relationships and documents every deployed field, nested children, examples,
row counts, entity-country mappings and omissions. Nested fields use BigQuery's
`fields` key (rather than the original incomplete `subfields` convention).
BOOL/INT64/FLOAT64 are aliases for BigQuery API BOOLEAN/INTEGER/FLOAT.
The original supplied file remains unchanged under reference/project_schema.json.

Each field also includes sql_type, nullable, allowed_enum_values, enum_source and
observed_values, including nested fields. Declared enums are distinct from observed
categories. Non-ID fields with up to 50 distinct scalar values list complete values
and frequencies; high-cardinality fields and identifiers have counts and samples,
with ranges for numbers and dates. Nulls are counted separately. Semantic categories
such as risk_typology_id and party_supplementary_data_id are enumerated.
Run `python3 enrich_schema.py` to refresh this documentation from local data without
changing BigQuery or regenerating records. Normal generation also enriches the JSON.

`python3 check_naming.py` verifies the mapping and compares all non-naming values
against baseline files. `python3 migrate_identifiers.py` performs exact live-data
preflight checks, dated backups, staged loading and verification, then table copy
replacements. It preserves schemas and expiration settings and supports resuming.
Backup: aml_demo_backup_20260919_v1. Staging: aml_demo_stage_20260919_v2.
The migration stops on any unexpected live contents. Backups are retained with
BigQuery Sandbox expiration; no tables are deleted by this workflow.

## Files and loading

Run `python3 generate.py` to reproduce the package with fixed seed 20260919.
Run `python3 load_bigquery.py --plan` to validate file checksums and display the load target.
In authenticated Google Cloud Shell, run `python3 load_bigquery.py` from this directory.
The loader uses the current gcloud identity, does not store tokens, and refuses to overwrite or append to unrecognized tables.
Loads use WRITE_EMPTY and stable job IDs. A retry resumes completed loads and verifies row counts.
The loader performs read-only BigQuery checks with a 100 MB query billing cap and writes receipts under `reports/`.

`data/`: BigQuery newline-delimited JSON, one file per table.
`schemas/`: explicit nested BigQuery schemas, no autodetection required.
`companion/`: transaction original currency, fictional fixed FX rates and scenario ground truth. These files are NOT loaded as extra AML tables.
`reports/validation.json`: local validation result and expected counts/totals.
`reference/`: unchanged supplied schema, official input schema snapshot, and original project KPI SQL.

## Design

850 customers are present at the opening snapshot; 150 join during the period.
There are 100 address-history updates and 40 customer exits, yielding 1,140 Party rows for 1,000 people.
Each party has one primary account. Transactions and interaction events occur only while that account is active.
There are 90 customers with synthetic activity scenarios: 30 cash bursts, 30 rapid incoming/outgoing movements, and 30 cross-border bursts.
Scenario assignment is independent of country, gender, nationality and occupation.
There are 120 investigations, 80 closed and 40 open, 45 SAR events and 20 AML-related exits.
The other 20 exits are ordinary customer departures.
All transaction amounts are positive USD-normalized money structs, including cents in `nanos`.
Directions represent inflows/outflows; summing both is gross account turnover, not net flows.
Counterparties are external fictional entities with null internal account IDs. No unpaired internal transfers are implied.
Local currencies are HKD, GBP, INR, TWD, EUR and PLN, using explicitly fictional fixed conversion rates.
Monthly risk scores and explanations are simulated for active customers; metadata is illustrative, not measured model performance.
Risk period end timestamps are exclusive next-month boundaries in UTC: August's boundary is September 1.
CommercialPartiesRegistration is intentionally empty because the agreed scope is retail-only.

## Schema decisions and limitations

Google's official input JSON completes nested structures missing from the supplied file.
Party.civil_status_code is omitted pending confirmation: supplied STRUCT conflicts with Google's STRING. This field is optional.
The supplied stricter REQUIRED modes for AccountPartyLink.role and Transaction.normalized_booked_amount are retained; all rows satisfy them.
The original source files are unchanged. No chatbot code or graph was modified.

This is a demo dataset, not yet a validated AML AI training dataset. Eight months is insufficient for the full training/tuning workflow.
Google recommends 36 months for a first sample test; exact requirements depend on engine version and operation.
AML AI service access, supported region, registration, engine choice, label coverage and sufficient historical data must be verified separately before actual service use.
BigQuery Sandbox tables expire under its retention policy; loading does not activate billing or AML AI.

Original KPI queries require care: historical Party joins can duplicate results, the exit query joins all score periods, and summing units alone drops cents.
Use an as-of Party record, select the latest score before exit, and add nanos / 1e9 for monetary totals.
The project SQL is preserved as reference; this package does not change the chatbot or claim its queries were fixed.

Sources:
- https://docs.cloud.google.com/financial-services/anti-money-laundering/docs/reference/schemas/aml-input-data-model
- https://docs.cloud.google.com/static/financial-services/anti-money-laundering/docs/reference/schemas/aml-input-data-model.json
- https://docs.cloud.google.com/financial-services/anti-money-laundering/docs/reference/schemas/aml-output-data-model
- https://docs.cloud.google.com/financial-services/anti-money-laundering/docs/understand-data-scope-duration
