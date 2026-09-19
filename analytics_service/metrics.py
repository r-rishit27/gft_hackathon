"""Versioned starter KPI definitions; domain-owner approval is still required."""

from dataclasses import dataclass
import re

from .errors import AnalyticsError


@dataclass(frozen=True)
class Metric:
    id: str
    question: str
    aliases: tuple[str, ...]
    sql: str
    units: dict[str, str]
    chart: str
    dimension: str | None = None
    series: str | None = None


METRICS = (
    Metric("transaction_trend", "Show monthly transaction volume and value by direction", (
        "monthly transaction volume", "transaction volume and value trend",
    ), """
SELECT DATE_TRUNC(DATE(book_time), MONTH) AS month, direction,
       COUNT(*) AS transaction_count,
       SUM(CAST(normalized_booked_amount.units AS NUMERIC)
           + CAST(normalized_booked_amount.nanos AS NUMERIC) / 1000000000) AS amount_usd
FROM Transaction
WHERE COALESCE(is_entity_deleted, FALSE) = FALSE
GROUP BY month, direction
ORDER BY month, direction
""", {"transaction_count": "transactions", "amount_usd": "USD"}, "line", "month", "direction"),
    Metric("sar_volume", "How many cases had SAR filings each month?", (
        "monthly sar volume", "sar filing volume by month",
    ), """
SELECT DATE_TRUNC(DATE(event_time), MONTH) AS month,
       COUNT(DISTINCT risk_case_id) AS sar_cases
FROM RiskCaseEvent WHERE type = 'AML_SAR'
GROUP BY month ORDER BY month
""", {"sar_cases": "cases"}, "bar", "month"),
    Metric("case_backlog", "Show open and closed investigation cases", (
        "case backlog", "open versus closed cases",
    ), """
WITH status AS (
 SELECT risk_case_id,
 MAX(IF(type = 'AML_PROCESS_START', event_time, NULL)) AS started_at,
 MAX(IF(type = 'AML_PROCESS_END', event_time, NULL)) AS ended_at
 FROM RiskCaseEvent GROUP BY risk_case_id
)
SELECT COUNTIF(started_at IS NOT NULL AND (ended_at IS NULL OR started_at > ended_at)) AS open_cases,
       COUNTIF(started_at IS NOT NULL AND ended_at >= started_at) AS closed_cases
FROM status
""", {"open_cases": "cases", "closed_cases": "cases"}, "kpi"),
    Metric("risk_trend", "Show the average risk score by reporting period", (
        "average risk score trend", "risk score trend",
    ), """
SELECT risk_period_end_time AS period, AVG(risk_score) AS average_risk_score,
       COUNT(DISTINCT IF(risk_score IS NOT NULL, party_id, NULL)) AS scored_customers
FROM RiskScores GROUP BY period ORDER BY period
""", {"average_risk_score": "score (0-1)", "scored_customers": "customers"}, "line", "period"),
    Metric("customer_count", "How many active customers were there at the end of August 2026?", (
        "active customers at dataset end", "latest active customer count",
    ), """
WITH latest AS (
 SELECT party_id, is_entity_deleted, exit_date, join_date,
 ROW_NUMBER() OVER (PARTITION BY party_id ORDER BY validity_start_time DESC) AS row_num
 FROM Party WHERE validity_start_time < TIMESTAMP('2026-09-01 00:00:00+00')
)
SELECT COUNT(*) AS active_customers FROM latest
WHERE row_num = 1 AND COALESCE(is_entity_deleted, FALSE) = FALSE
AND join_date < DATE('2026-09-01')
AND (exit_date IS NULL OR exit_date >= DATE('2026-09-01'))
""", {"active_customers": "customers"}, "kpi"),
    Metric("alert_conversion", "What proportion of alerted cases had a SAR?", (
        "alert to sar conversion rate", "alert to sar rate",
    ), """
WITH alerts AS (
 SELECT DISTINCT risk_case_id FROM RiskCaseEvent
 WHERE type IN ('AML_ALERT_GOOGLE', 'AML_ALERT_LEGACY', 'AML_ALERT_ADHOC',
                'AML_ALERT_EXPLORATORY', 'AML_ALERT_EXTERNAL')
), sars AS (
 SELECT DISTINCT risk_case_id FROM RiskCaseEvent WHERE type = 'AML_SAR'
)
SELECT COUNT(DISTINCT a.risk_case_id) AS alerted_cases,
COUNT(DISTINCT s.risk_case_id) AS sar_cases,
SAFE_DIVIDE(COUNT(DISTINCT s.risk_case_id), COUNT(DISTINCT a.risk_case_id)) AS conversion_rate
FROM alerts a LEFT JOIN sars s ON a.risk_case_id = s.risk_case_id
""", {"alerted_cases": "cases", "sar_cases": "cases", "conversion_rate": "ratio"}, "kpi"),
)


def normalize_question(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()


def resolve_metric(question: str) -> Metric:
    question = normalize_question(question)
    for metric in METRICS:
        if question in {normalize_question(q) for q in (metric.question, *metric.aliases)}:
            return metric
    raise AnalyticsError(
        "clarification_required",
        "Choose a supported KPI question. Custom filters and unreviewed metric meanings are not enabled in v1.",
    )
