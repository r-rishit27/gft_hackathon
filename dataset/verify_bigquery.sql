-- Every returned violation count must be zero. Read-only; no data changes.
WITH parties AS (
  SELECT DISTINCT party_id FROM `gen-lang-client-0810987953.aml_demo.Party`
), accounts AS (
  SELECT DISTINCT account_id FROM `gen-lang-client-0810987953.aml_demo.AccountPartyLink`
)
SELECT 'orphan_transactions' AS check_name, COUNT(*) AS violations
FROM `gen-lang-client-0810987953.aml_demo.Transaction` t
LEFT JOIN accounts a USING (account_id) WHERE a.account_id IS NULL
UNION ALL
SELECT 'orphan_cases', COUNT(*) FROM `gen-lang-client-0810987953.aml_demo.RiskCaseEvent` r
LEFT JOIN parties p USING (party_id) WHERE p.party_id IS NULL
UNION ALL
SELECT 'invalid_money', COUNT(*) FROM `gen-lang-client-0810987953.aml_demo.Transaction`
WHERE normalized_booked_amount.currency_code != 'USD' OR normalized_booked_amount.units < 0
   OR normalized_booked_amount.nanos NOT BETWEEN 0 AND 999999999
UNION ALL
SELECT 'invalid_scores', COUNT(*) FROM `gen-lang-client-0810987953.aml_demo.RiskScores`
WHERE risk_score NOT BETWEEN 0 AND 1
UNION ALL
SELECT 'customer_count_mismatch', ABS(COUNT(*) - 1000) FROM parties
UNION ALL
SELECT 'transaction_count_mismatch', ABS(COUNT(*) - 50000)
FROM `gen-lang-client-0810987953.aml_demo.Transaction`
UNION ALL
SELECT 'missing_explanations', COUNT(*) FROM `gen-lang-client-0810987953.aml_demo.RiskScores` r
LEFT JOIN `gen-lang-client-0810987953.aml_demo.Explainability` e USING (party_id, risk_period_end_time)
WHERE e.party_id IS NULL;
