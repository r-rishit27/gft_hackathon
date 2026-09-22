from decimal import Decimal, InvalidOperation

from .bigquery_client import QueryResult
from .metrics import Metric


def build_dashboard(result: QueryResult, metric: Metric) -> dict:
    if result.truncated:
        return {"state": "partial", "charts": [], "insights": [
            "Results are incomplete. Charts and aggregate insights are suppressed."
        ]}
    if not result.rows:
        return {"state": "empty", "charts": [], "insights": ["No matching records."]}
    charts = []
    insights = []
    names = {column["name"] for column in result.columns}
    for measure, unit in metric.units.items():
        if measure not in names:
            continue
        if metric.chart == "kpi" and len(result.rows) == 1:
            value = result.rows[0].get(measure)
            insights.append(f"{measure.replace('_', ' ')}: {value if value is not None else 'not available'} ({unit}).")
            charts.append({"kind": "kpi", "measure": measure, "unit": unit, "value": value})
        elif metric.dimension and metric.dimension in names:
            charts.append({"kind": metric.chart, "x": metric.dimension, "y": measure,
                           "series": metric.series, "unit": unit, "timezone": "UTC"})
            groups = sorted({str(row.get(metric.series)) for row in result.rows}) if metric.series else [None]
            for group in groups:
                rows = sorted((r for r in result.rows if group is None or str(r.get(metric.series)) == group),
                              key=lambda r: str(r[metric.dimension]))
                if len(rows) < 2 or rows[-1].get(measure) is None or rows[-2].get(measure) is None:
                    continue
                try:
                    latest, previous = Decimal(str(rows[-1][measure])), Decimal(str(rows[-2][measure]))
                    change = latest - previous
                    if not latest.is_finite() or not change.is_finite():
                        continue
                    prefix = f"{group}: " if group is not None else ""
                    insights.append(f"{prefix}{measure.replace('_', ' ')} is {latest} {unit}; "
                                    f"change from the previous available point: {change:+} {unit}.")
                except InvalidOperation:
                    continue
    return {"state": "complete", "charts": charts, "insights": insights}
