-- Read-only examples for the generated dataset. All risk outputs are simulated.

-- Current customers by country; select one Party version per customer.
WITH latest_party AS (
  SELECT * FROM `gen-lang-client-0810987953.aml_demo.Party`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY party_id ORDER BY validity_start_time DESC) = 1
)
SELECT r.region_code, COUNT(*) AS customers, COUNTIF(exit_date IS NULL) AS active_customers
FROM latest_party p, UNNEST(p.residencies) r
GROUP BY r.region_code ORDER BY r.region_code;

-- Gross transaction value, including fractional units.
SELECT DATE_TRUNC(DATE(book_time), MONTH) AS month, type, direction,
       COUNT(*) AS transaction_count,
       SUM(CAST(normalized_booked_amount.units AS NUMERIC)
         + CAST(normalized_booked_amount.nanos AS NUMERIC) / 1000000000) AS gross_value_usd
FROM `gen-lang-client-0810987953.aml_demo.Transaction`
GROUP BY month, type, direction ORDER BY month, type, direction;

-- Cases, counted once rather than once per event.
WITH cases AS (
  SELECT risk_case_id,
         COUNTIF(type = 'AML_PROCESS_START') > 0 AS started,
         COUNTIF(type = 'AML_PROCESS_END') > 0 AS closed,
         COUNTIF(type = 'AML_SAR') > 0 AS sar
  FROM `gen-lang-client-0810987953.aml_demo.RiskCaseEvent`
  GROUP BY risk_case_id
)
SELECT COUNTIF(started) AS investigations, COUNTIF(started AND NOT closed) AS open_cases,
       COUNTIF(closed) AS closed_cases, COUNTIF(sar) AS sar_cases
FROM cases;
