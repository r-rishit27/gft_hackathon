# AML Text-to-SQL Pipeline — Test Report

**System under test:** KAG (knowledge-augmented generation) pipeline in `text2sql_falkordb.py` — FalkorDB
knowledge-graph retrieval → `gaussalgo/T5-LM-Large-text2sql-spider` (fine-tuned on `finetuned_model/`) →
exemplar-retrieval shortcut → KG-scoped table/column repair → final validation against the canonical
`aml_data_model_schema.json` (`schema_validator.py`).

**Date of this run:** 2026-09-19
**Evaluator:** `evaluate_text2sql.py`

---

## 1. Metrics

| Metric | Definition |
|---|---|
| `exact_match` | Normalized-whitespace, case-insensitive string equality against the gold SQL. Informative but a poor standalone similarity measure — a semantically correct query with different aliasing/column order will show as a miss. |
| `table_recall` | Fraction of gold tables referenced in the gold SQL that also appear in the generated SQL. |
| `table_precision` | Fraction of tables in the generated SQL that are actually gold tables. |
| `schema_valid` | Every table, column, and CTE reference in the generated SQL resolves against the canonical `aml_data_model_schema.json` (after automatic repair). This is the **safety metric** — it answers "would this query actually run," independent of whether it answers the question correctly. |

---

## 2. Summary

| Test suite | File | Questions | Exact match | Table recall | Table precision | Schema valid |
|---|---|---|---|---|---|---|
| Exemplar bank | `eval_testcases.json` | 13 | **13/13 (100%)** | 100% | 100% | **13/13 (100%)** |
| Held-out | `eval_heldout.json` | 6 | 3/6 (50%) | 83% | 75% | 5/6 (83%) |
| New schema-strict | `eval_new_queries.json` | 12 | 2/12 (17%) | 92% | 83% | 9/12 (75%) |
| **Combined** | — | **31** | **18/31 (58%)** | **91%** | **86%** | **27/31 (87%)** |

**Reading these numbers correctly:** the exemplar-bank suite scores 100% because every one of its questions
is, by construction, an exact or near-exact match to a verified answer in the exemplar-retrieval cache — it
is testing the retrieval shortcut and the schema validator's tolerance for legitimate SQL (CTEs, joins,
window functions), not the model's raw generation ability. **The held-out and new-queries suites are the
honest signal on model quality**, since every question in them requires actual generation.

---

## 3. Detailed results

### 3.1 Exemplar bank (`eval_testcases.json`) — 13/13 exact, 13/13 schema-valid

All 13 questions hit `exemplar_retrieval` and returned the verified gold SQL byte-for-byte. This suite
validates two things: (a) the retrieval-shortcut path works, and (b) the schema validator correctly passes
legitimate complex SQL — including two queries using a `WITH ... AS (...)` CTE, `JOIN`s, `SAFE_DIVIDE`,
`APPROX_QUANTILES`, and a self-join — without false-flagging any of it.

| # | Question | Result |
|---|---|---|
| 1 | How many SARs were filed each month? | ✅ exact, valid |
| 2 | What percentage of alerted cases resulted in a SAR being filed? | ✅ exact, valid |
| 3 | How many risk cases are currently open versus closed? | ✅ exact, valid (CTE) |
| 4 | What is the average resolution time in days for closed risk cases? | ✅ exact, valid (CTE) |
| 5 | How many risk cases are there for each case type? | ✅ exact, valid |
| 6 | What percentage of parties have a risk score above 0.8 this period? | ✅ exact, valid |
| 7 | What is the average and 90th percentile risk score for each risk period? | ✅ exact, valid |
| 8 | Show the top 20 parties by risk score this period with their names | ✅ exact, valid |
| 9 | For parties that exited the bank, what is their average risk score at exit by month? | ✅ exact, valid |
| 10 | How many transactions were there each month by type and direction? | ✅ exact, valid |
| 11 | How many parties were registered at each registration time, broken down by tier? | ✅ exact, valid |
| 12 | Show the missingness and recall metrics for each resource | ✅ exact, valid |
| 13 | What percentage of alerts are adhoc or exploratory versus system generated? | ✅ exact, valid |

### 3.2 Held-out (`eval_heldout.json`) — 3/6 exact, 5/6 schema-valid

Includes an exact repeat and a reworded near-duplicate of a known question (to test exemplar-retrieval
robustness to paraphrasing), plus four genuinely novel questions the model has to generate from scratch.

| # | Question | Source | Exact | Valid | Notes |
|---|---|---|---|---|---|
| 1 | How many SARs were filed each month? | exemplar_retrieval | ✅ | ✅ | exact repeat |
| 2 | how many sars were filed per month | exemplar_retrieval | ✅ | ✅ | reworded — retrieval still matched |
| 3 | How many parties are there in total? | model_generation | ✅ | ✅ | trivial, model got it right |
| 4 | List the party id and account id for every account-party link | model_generation | ❌ | ✅ | model added an unnecessary `JOIN Party`; one column typo (`party_ide`) auto-repaired |
| 5 | How many login interaction events are there? | model_generation | ❌ | ❌ | model emitted `SELECT count(*) FROM login interaction event` — plain English words as if they were a table name. Correctly caught rather than silently returned. |
| 6 | Break down the risk case count by type | model_generation | ❌ | ✅ | correct table/column, but used `COUNT(*)` instead of `COUNT(DISTINCT risk_case_id)` — a semantic aggregation difference the validator isn't designed to catch (it checks schema grounding, not query logic) |

### 3.3 New schema-strict queries (`eval_new_queries.json`) — 2/12 exact, 9/12 schema-valid

Twelve fresh questions covering tables/columns not heavily exercised elsewhere: `Transaction`,
`AccountPartyLink.role`, `InteractionEvent`, `CommercialPartiesRegistration`, `RetailPartiesRegistration`,
`ExportedMetadata`, `PartySupplementaryData`, and `Party.type`/`join_date`.

| # | Question | Exact | Valid | Notes |
|---|---|---|---|---|
| 1 | How many transactions were made by card? | ❌ | ✅ | `TYPE = "CARD"` vs gold's `type = 'CARD'` — same meaning, different quote style/casing convention |
| 2 | List the account id and party id for every primary holder link | ❌ | ✅ | model used the wrong filter value (`party_id = 'Primary'` instead of `role = 'PRIMARY_HOLDER'`) — semantic error, not a schema error |
| 3 | How many interaction events were a password change? | ❌ | ✅ | correct logic, cosmetic differences only |
| 4 | What is the minimum and maximum risk score for each risk period? | ❌ | ✅ | exemplar retrieval matched a *different* stored question (avg/P90 percentile) closely enough in wording to return the wrong — but still schema-valid — cached answer |
| 5 | List the feature name and attribution value for each explainability record | ❌ | ✅ | model produced a structurally wrong join (`ExportedMetadata` instead of `UNNEST`), but every identifier it used still resolves |
| 6 | How many commercial parties are registered as large? | ❌ | ✅ | added a spurious `OR party_size = "SMALL"` clause — logic error, not schema error |
| 7 | How many retail parties are registered in total? | ✅ | ✅ | |
| 8 | Show the resource id and value of the importance metric for each model | ❌ | ✅ | dropped the `WHERE` filter, returned all resource types instead of just `Model`/`Importance` |
| 9 | How many parties are companies? | ❌ | ❌ | model output varies between runs depending on which tables retrieval selects; produced malformed SQL (`SELECTION DISTINCT`) — correctly flagged |
| 10 | What is the earliest join date among all parties? | ❌ | ❌ | hallucinated a nonexistent `RegisteredPartiesExport.earliest_join_date` column and a `date` "table" — correctly flagged |
| 11 | How many risk case events are an AML exit? | ❌ | ❌ | used the enum value `AML_EXIT` as if it were a column name (`WHERE AML_EXIT = 'Yes'`) — correctly flagged |
| 12 | List the party id and supplementary data payload for every party supplementary data record | ✅ | ✅ | |

---

## 4. Bugs found and fixed during this testing pass

Testing wasn't just measurement — it surfaced four real defects in the pipeline, all fixed and re-verified
with no regressions on the suites above:

| # | Bug | Root cause | Fix |
|---|---|---|---|
| 1 | Double-quoted literal values (`TYPE = "CARD"`) were flagged as schema violations | The literal-masking regex only matched single-quoted strings (`'...'`); BigQuery treats both `'` and `"` as string delimiters | Extended the mask regex to cover both quote styles |
| 2 | `extract_enum_hint` silently dropped the first item of a bare two-item list (`"COMPANY or CONSUMER"` → only extracted `CONSUMER`) | Regex only matched a word followed by a comma, or preceded by `"or"` — missed the case where the first word is immediately followed by `"or"` with no comma | Added a third pattern (`word + " or"`); consolidated the duplicate implementation in `text2sql_falkordb.py` into one canonical version in `schema_validator.py` |
| 3 | `WITH case_status AS (...)` queries flagged the CTE name and its internal column aliases as schema violations | The validator had no concept of CTEs — it treated `case_status` as an unrecognized table | Added CTE-name detection (`WITH x AS (`, `, y AS (`) and exempted those names from table/identifier checks |
| 4 | The *same* hallucinated column, differently cased in two places in one query (`risk_type` vs `risk_Type`), was fixed in one spot and left broken in the other | `difflib.SequenceMatcher` penalizes a single case mismatch far more than intuition suggests — `risk_Type` vs `type` scores 0.46 (fails a 0.6 cutoff) while `risk_type` vs `type` scores 0.62 (passes) — so whether an identifier got auto-corrected came down to the luck of its casing | Added `_fuzzy_match_ci()`, which matches against lowercased candidates and returns the real casing; applied to all three fuzzy-match sites (tables, qualified columns, bare identifiers) |

A fifth issue was found in the underlying data rather than the code: two "gold" answers in
`eval_testcases.json` referenced a `started_at`/`ended_at` CTE alias without the CTE that defines it — an
authoring mistake from when those queries were trimmed down earlier in this project. Both were restored to
valid, complete SQL.

---

## 5. What the schema-validation layer catches vs. doesn't

**Catches (by design):**
- Hallucinated table names (`RiskCase` → `RiskCaseEvent`)
- Hallucinated or misspelled column names, qualified or bare
- Enum-value casing/token drift (`type = "Company"` → `type = "COMPANY"`)
- Nonexistent tables masquerading as plain English (`FROM login interaction event`)
- A hallucinated enum value used as if it were a column name (`WHERE AML_EXIT = 'Yes'`)

**Does not catch (out of scope by design):**
- Wrong aggregation logic (`COUNT(*)` vs `COUNT(DISTINCT ...)`)
- Wrong filter *value* when the column is real (`role = 'Primary'` instead of `'PRIMARY_HOLDER'`, when
  `role` genuinely exists and `'Primary'` genuinely parses as a string)
- Missing or extra `WHERE`/`JOIN` conditions that don't reference invalid identifiers
- Whether the query actually answers the natural-language question at all

In short: **`schema_valid: true` means the SQL is safe to attempt to run against the real schema — it does
not mean the SQL is correct.** Every generated query should still be reviewed before use, exactly as the
composer footer and the read-only-SQL system prompt both state.

---

## 6. Known limitations

- **Small fine-tuning set.** `finetuned_model/` was trained on only 13 examples (the exemplar bank itself) —
  enough to reliably answer those 13 patterns via retrieval, not enough for the underlying model to
  generalize well to novel phrasing. The held-out/new-queries results above are the honest measure of that.
- **CPU-only inference.** No GPU was available; this bounded how much fine-tuning and evaluation could
  practically be run (see `finetune_text2sql.py`'s epoch/optimizer choices).
- **Lexical (not semantic) retrieval and exemplar matching.** Both the table-retrieval step and the
  exemplar-similarity check use token-overlap (Jaccard) scoring, not embeddings. This is why item #4 in
  §3.3 matched the wrong cached exemplar — "minimum and maximum" vs. "average and 90th percentile" share
  enough surrounding vocabulary to cross a naive similarity threshold. An embedding-based retriever would
  reduce this failure mode but wasn't pursued here to avoid another heavy model download on this
  CPU-constrained machine.
- **Non-determinism from unordered graph reads.** FalkorDB doesn't guarantee row order without `ORDER BY`;
  this was fixed for the table-retrieval ranking (tie-break by name), but any future retrieval query built
  without explicit ordering could reintroduce this class of bug.

---

## 7. Reproducing this report

```bash
python evaluate_text2sql.py --model ./finetuned_model --testcases eval_testcases.json  --out eval_testcases_results.json
python evaluate_text2sql.py --model ./finetuned_model --testcases eval_heldout.json    --out heldout_results.json
python evaluate_text2sql.py --model ./finetuned_model --testcases eval_new_queries.json --out new_queries_results.json
```

Each run prints the summary table above to stdout and writes full per-question detail (gold SQL, predicted
SQL, corrections applied, schema violations) to the named JSON file.
