from analytics_service.bigquery_client import QueryResult
from analytics_service.explore_dashboard import build_exploration


def result(columns, rows, truncated=False):
    return QueryResult([{"name": n, "type": t} for n, t in columns], rows, "test", 0, truncated)


def test_numeric_identifier_is_not_a_measure():
    data = result([("party_id", "INTEGER"), ("risk_score", "FLOAT")], [{"party_id": 42, "risk_score": .5}])
    assert build_exploration(data, "SELECT party_id, risk_score FROM RiskScores")[0]["charts"] == []


def test_unrepresented_dimensions_use_table():
    data = result([("country", "STRING"), ("direction", "STRING"), ("type", "STRING"), ("n", "INTEGER")],
                  [{"country": "IN", "direction": "CREDIT", "type": "WIRE", "n": 5}])
    assert build_exploration(data, "SELECT country, direction, type, COUNT(*) AS n FROM Transaction GROUP BY 1,2,3")[0]["charts"] == []


def test_temporal_results_use_line_and_categories_use_bar():
    for dimension, kind in [("DATE", "line"), ("STRING", "bar")]:
        data = result([("period", dimension), ("n", "INTEGER")], [{"period": "2026-01-01", "n": 2}, {"period": "2026-02-01", "n": 3}])
        dashboard, _ = build_exploration(data, "SELECT period, COUNT(*) AS n FROM Transaction GROUP BY period")
        assert dashboard["charts"][0]["kind"] == kind


def test_single_measure_and_partial_results():
    data = result([("n", "INTEGER")], [{"n": 0}])
    assert build_exploration(data, "SELECT COUNT(*) AS n FROM Transaction")[0]["charts"][0]["value"] == 0
    data.truncated = True
    assert build_exploration(data, "SELECT COUNT(*) AS n FROM Transaction")[0]["charts"] == []
