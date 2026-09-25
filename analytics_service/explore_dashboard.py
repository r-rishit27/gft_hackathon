"""Deterministic, result-driven charts. No model-written rendering code."""
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp

NUMERIC = {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC", "DECIMAL", "DOUBLE", "BIGINT"}
TEMPORAL = {"DATE", "TIMESTAMP", "DATETIME"}


def units_for(sql, columns):
    units = {c["name"]: "value" for c in columns if c["type"] in NUMERIC}
    tree = sqlglot.parse_one(sql, read="bigquery")
    for expression in tree.selects:
        name = expression.alias_or_name
        if name not in units:
            continue
        text = expression.sql().lower()
        divisions = list(expression.find_all(exp.Div, exp.SafeDivide))
        if any(any(d.expression.find_all(exp.Count, exp.CountIf, exp.Sum)) for d in divisions):
            units[name] = "ratio"
        elif any(expression.find_all(exp.Count, exp.CountIf)):
            units[name] = "count"
        elif "normalized_booked_amount" in text and "units" in text:
            units[name] = "USD"
        elif "risk_score" in text:
            units[name] = "risk score"
    return units


def build_exploration(result, sql):
    units = units_for(sql, result.columns)
    if result.truncated:
        return {"state": "partial", "charts": [], "insights": ["Result limit reached. Charts are suppressed to avoid showing partial data as complete."]}, units
    if not result.rows:
        return {"state": "empty", "charts": [], "insights": ["No matching records were returned by BigQuery."]}, units
    scalar = [c for c in result.columns if c.get("mode") != "REPEATED"]
    identifiers = {c["name"] for c in scalar if c["name"].lower() == "id" or c["name"].lower().endswith("_id")}
    numbers = [c["name"] for c in scalar if c["type"] in NUMERIC and c["name"] not in identifiers]
    times = [c["name"] for c in scalar if c["type"] in TEMPORAL]
    categories = [c["name"] for c in scalar if c["type"] in {"STRING", "VARCHAR", "BOOLEAN", "BOOL"}]
    charts, insights = [], []
    if len(result.rows) == 1 and not categories and not times and not identifiers:
        for name in numbers[:6]:
            value = result.rows[0].get(name)
            charts.append({"kind": "kpi", "measure": name, "unit": units[name], "value": value})
            insights.append(f"{name.replace('_', ' ')}: {value if value is not None else 'not available'} ({units[name]}).")
    elif numbers and (times or categories) and not identifiers:
        dimension = (times or categories)[0]
        series = next((c for c in categories if c != dimension and len({str(r.get(c)) for r in result.rows}) <= 8), None)
        grain = [(str(r.get(dimension)), str(r.get(series)) if series else "") for r in result.rows]
        used_dimensions = {dimension, series} - {None}
        complete_dimensions = set(times + categories) <= used_dimensions
        if complete_dimensions and len(set(grain)) == len(grain) and (times or len(result.rows) <= 30):
            for name in numbers[:4]:
                charts.append({"kind": "line" if times else "bar", "x": dimension, "y": name,
                               "series": series, "unit": units[name], "timezone": "UTC"})
                values = []
                for row in result.rows:
                    try:
                        value = Decimal(str(row.get(name)))
                        if value.is_finite():
                            values.append((value, row))
                    except InvalidOperation:
                        pass
                if values:
                    value, row = max(values, key=lambda pair: pair[0])
                    group = f", {row[series]}" if series else ""
                    unit = "" if units[name] in {"count", "value"} else f" {units[name]}"
                    insights.append(f"Highest returned {name.replace('_', ' ')}: {value:,}{unit} at {row[dimension]}{group}.")
        else:
            insights.append("The returned dimensions do not form a unique chart series. The complete table is shown without re-aggregating it.")
    if not charts and not insights:
        insights.append(f"BigQuery returned {len(result.rows)} rows. Review the table and executed SQL below.")
    return {"state": "complete", "charts": charts, "insights": insights}, units
