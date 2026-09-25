-- AML KPI queries for senior management reporting
-- Built against the tables in aml_data_model_schema.json
-- (input_data_model: Party, AccountPartyLink, Transaction, RiskCaseEvent, PartySupplementaryData;
--  output_data_model: RiskScores, Explainability, RegisteredPartiesExport, ExportedMetadata)
-- Dialect: BigQuery Standard SQL (AML AI's tables live in BigQuery).

-- ============================================================
-- 1. SAR (Suspicious Activity Report) filing volume, by month
-- ============================================================
SELECT
  DATE_TRUNC(DATE(event_time), MONTH) AS report_month,
  COUNT(*) AS sar_count
FROM `RiskCaseEvent`
WHERE type = 'AML_SAR'
GROUP BY report_month
ORDER BY report_month;

-- ============================================================
-- 2. Alert-to-SAR conversion rate (how many alerts end up as SARs)
-- ============================================================
WITH alerts AS (
  SELECT risk_case_id, COUNT(*) AS alert_count
  FROM `RiskCaseEvent`
  WHERE type IN ('AML_ALERT_GOOGLE', 'AML_ALERT_LEGACY', 'AML_ALERT_ADHOC',
                 'AML_ALERT_EXPLORATORY', 'AML_ALERT_EXTERNAL')
  GROUP BY risk_case_id
),
sars AS (
  SELECT DISTINCT risk_case_id
  FROM `RiskCaseEvent`
  WHERE type = 'AML_SAR'
)
SELECT
  COUNT(DISTINCT a.risk_case_id) AS total_alerted_cases,
  COUNT(DISTINCT s.risk_case_id) AS cases_resulting_in_sar,
  SAFE_DIVIDE(COUNT(DISTINCT s.risk_case_id), COUNT(DISTINCT a.risk_case_id)) AS alert_to_sar_rate
FROM alerts a
LEFT JOIN sars s USING (risk_case_id);

-- ============================================================
-- 3. Open vs. closed risk cases (case backlog)
-- ============================================================
WITH case_status AS (
  SELECT
    risk_case_id,
    MAX(IF(type = 'AML_PROCESS_START', event_time, NULL)) AS started_at,
    MAX(IF(type = 'AML_PROCESS_END', event_time, NULL)) AS ended_at
  FROM `RiskCaseEvent`
  GROUP BY risk_case_id
)
SELECT
  COUNTIF(ended_at IS NULL) AS open_cases,
  COUNTIF(ended_at IS NOT NULL) AS closed_cases,
  COUNT(*) AS total_cases
FROM case_status;

-- ============================================================
-- 4. Average / median case resolution time (days), closed cases only
-- ============================================================
WITH case_status AS (
  SELECT
    risk_case_id,
    MIN(IF(type = 'AML_PROCESS_START', event_time, NULL)) AS started_at,
    MAX(IF(type = 'AML_PROCESS_END', event_time, NULL)) AS ended_at
  FROM `RiskCaseEvent`
  GROUP BY risk_case_id
)
SELECT
  AVG(TIMESTAMP_DIFF(ended_at, started_at, HOUR)) / 24.0 AS avg_resolution_days,
  APPROX_QUANTILES(TIMESTAMP_DIFF(ended_at, started_at, HOUR), 2)[OFFSET(1)] / 24.0 AS median_resolution_days
FROM case_status
WHERE started_at IS NOT NULL AND ended_at IS NOT NULL;

-- ============================================================
-- 5. Case volume by risk typology (which typologies drive the workload)
-- ============================================================
SELECT
  rtm.risk_typology_id,
  COUNT(DISTINCT rce.risk_case_id) AS case_count
FROM `RiskCaseEvent` rce,
  UNNEST(risk_typology_measurements) AS rtm
GROUP BY rtm.risk_typology_id
ORDER BY case_count DESC;

-- ============================================================
-- 6. High-risk party count and % of book above a risk threshold
-- ============================================================
SELECT
  risk_period_end_time,
  COUNTIF(risk_score >= 0.8) AS high_risk_parties,
  COUNT(*) AS total_scored_parties,
  SAFE_DIVIDE(COUNTIF(risk_score >= 0.8), COUNT(*)) AS pct_high_risk
FROM `RiskScores`
GROUP BY risk_period_end_time
ORDER BY risk_period_end_time DESC;

-- ============================================================
-- 7. Average / top-decile risk score trend over time
-- ============================================================
SELECT
  risk_period_end_time,
  AVG(risk_score) AS avg_risk_score,
  APPROX_QUANTILES(risk_score, 10)[OFFSET(9)] AS p90_risk_score
FROM `RiskScores`
GROUP BY risk_period_end_time
ORDER BY risk_period_end_time;

-- ============================================================
-- 8. Top N riskiest parties this period, with driving feature attributions
-- ============================================================
SELECT
  rs.party_id,
  p.name,
  rs.risk_score,
  ARRAY_AGG(STRUCT(attr.feature, attr.attribution) ORDER BY attr.attribution DESC LIMIT 3) AS top_drivers
FROM `RiskScores` rs
JOIN `Explainability` ex
  ON rs.party_id = ex.party_id AND rs.risk_period_end_time = ex.risk_period_end_time,
  UNNEST(ex.attributions) AS attr
JOIN `Party` p ON p.party_id = rs.party_id
WHERE rs.risk_period_end_time = (SELECT MAX(risk_period_end_time) FROM `RiskScores`)
GROUP BY rs.party_id, p.name, rs.risk_score
ORDER BY rs.risk_score DESC
LIMIT 20;

-- ============================================================
-- 9. Customer exits linked to high risk score (attrition among risky parties)
-- ============================================================
SELECT
  DATE_TRUNC(p.exit_date, MONTH) AS exit_month,
  COUNT(*) AS exited_parties,
  AVG(rs.risk_score) AS avg_risk_score_at_exit
FROM `Party` p
JOIN `RiskScores` rs ON p.party_id = rs.party_id
WHERE p.exit_date IS NOT NULL
GROUP BY exit_month
ORDER BY exit_month;

-- ============================================================
-- 10. Transaction volume and value trend, by type and direction
-- ============================================================
SELECT
  DATE_TRUNC(DATE(book_time), MONTH) AS txn_month,
  type,
  direction,
  COUNT(*) AS txn_count,
  SUM(normalized_booked_amount.units) AS total_value
FROM `Transaction`
GROUP BY txn_month, type, direction
ORDER BY txn_month, type;

-- ============================================================
-- 11. New customer registrations by tier (retail vs. commercial small/large)
-- ============================================================
SELECT
  DATE_TRUNC(DATE(registration_or_uptier_time), MONTH) AS registration_month,
  COALESCE(party_size, 'RETAIL') AS tier,
  COUNT(*) AS registered_parties
FROM `RegisteredPartiesExport`
GROUP BY registration_month, tier
ORDER BY registration_month, tier;

-- ============================================================
-- 12. Model quality KPIs for management: missingness & recall trend
-- ============================================================
SELECT
  resource_type,
  resource_id,
  name AS metric,
  value
FROM `ExportedMetadata`
WHERE name IN ('Missingness', 'ObservedRecallValues', 'ObservedRecallValuesPerTypology')
ORDER BY resource_type, resource_id, metric;

-- ============================================================
-- 13. Exploratory/adhoc alert share vs. system-generated alerts
--     (signals analyst manual workload vs. model-driven detection)
-- ============================================================
SELECT
  type AS alert_type,
  COUNT(*) AS alert_count,
  SAFE_DIVIDE(COUNT(*), SUM(COUNT(*)) OVER ()) AS pct_of_all_alerts
FROM `RiskCaseEvent`
WHERE type IN ('AML_ALERT_GOOGLE', 'AML_ALERT_LEGACY', 'AML_ALERT_ADHOC',
               'AML_ALERT_EXPLORATORY', 'AML_ALERT_EXTERNAL')
GROUP BY alert_type
ORDER BY alert_count DESC;
