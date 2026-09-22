"""Isolated doubles are tests only; the local application never uses them."""
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from analytics_service.app import create_app
from analytics_service.bigquery_client import QueryResult
from analytics_service.catalog import load_catalog
from analytics_service.demo import fixture_settings
from analytics_service.explore_dashboard import build_exploration
from analytics_service.model_client import ModelClient
from pipeline.bigquery_schema import validate_candidate


def test_narrow_dialect_normalization_keeps_all_statements_and_names():
    from pipeline.bigquery_schema import normalize_date_trunc
    sql, corrections = normalize_date_trunc("SELECT DATE_TRUNC('month', t.book_time) FROM Transaction t")
    assert "DATE_TRUNC(t.book_time, MONTH)" in sql
    assert corrections[0]["kind"] == "dialect"
    for query in ["SELECT 1; DROP TABLE Party", "SELECT DATE_TRUNC(t.book_time, MONTH) FROM Transaction t",
                  "SELECT DATE_TRUNC('bogus', t.book_time) FROM Transaction t"]:
        assert normalize_date_trunc(query) == (query, [])


def test_arbitrary_question_reaches_model_and_executes_validated_sql():
    question = "Please break down transaction counts by direction"
    seen = []

    class Model:
        def generate(self, value, allowed_columns):
            seen.append((value, allowed_columns))
            return "SELECT direction, COUNT(*) AS n FROM Transaction GROUP BY direction"

        def close(self):
            pass

    class Executor:
        def execute(self, query, scope, request_id):
            assert "aml_analytics_hk" in query.sql
            return QueryResult([{"name": "direction", "type": "STRING"}, {"name": "n", "type": "INTEGER"}],
                               [{"direction": "CREDIT", "n": 3}], "unit-test", 10, False)

    settings = fixture_settings("unit-test").model_copy(update={"query_mode": "freeform"})
    with TestClient(create_app(settings, Model(), Executor())) as client:
        response = client.post("/query", headers={"Authorization": "Bearer unit-test"}, json={"question": question})
    assert response.status_code == 200, response.text
    body = response.json()
    assert seen[0][0] == question
    assert "direction" in seen[0][1]["Transaction"]
    assert body["semantic_validation"] == "schema_and_policy_only"
    assert body["dashboard"]["charts"][0]["kind"] == "bar"
    assert body["units"]["n"] == "count"


def test_adapter_requests_execution_mode_not_exemplars():
    def handle(request):
        import json
        body = json.loads(request.content)
        assert body["execution_mode"] and not body["use_exemplars"]
        assert body["allowed_columns"] == {"Party": ["party_id"]}
        return httpx.Response(200, json={"sql": "SELECT COUNT(*) FROM Party", "schema_valid": True,
                                       "schema_violations": [], "additional_field": 1})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        sql = ModelClient("http://localhost/generate-sql", client).generate("How many?", {"Party": ["party_id"]})
        assert "COUNT" in sql


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) AS n FROM Transaction",
    "SELECT direction, SUM(normalized_booked_amount.units + normalized_booked_amount.nanos / 1e9) AS usd FROM Transaction GROUP BY direction",
    "SELECT COUNT(*) FROM `gen-lang-client-0810987953.aml_demo.Party`",
])
def test_google_sql_model_schema(sql):
    assert validate_candidate(sql, load_catalog()["tables"]) == []


@pytest.mark.parametrize("sql", [
    "DROP TABLE Party", "SELECT 1; SELECT 2", "SELECT imaginary FROM Party",
    "SELECT * FROM `other_project.aml_demo.Party`", "CLARIFICATION_REQUIRED",
])
def test_model_screen_rejects_invalid(sql):
    assert validate_candidate(sql, load_catalog()["tables"])


def test_chart_empty_partial_duplicate_and_null_behavior():
    sql = "SELECT direction, SUM(normalized_booked_amount.units) AS usd FROM Transaction GROUP BY direction"
    result = QueryResult([{"name": "direction", "type": "STRING"}, {"name": "usd", "type": "NUMERIC"}],
                         [{"direction": "DEBIT", "usd": "12.30"}, {"direction": "CREDIT", "usd": None}], "test", 0, False)
    dashboard, units = build_exploration(result, sql)
    assert units == {"usd": "USD"}
    assert "12.30" in dashboard["insights"][0]
    assert build_exploration(replace(result, truncated=True), sql)[0]["charts"] == []
    assert build_exploration(replace(result, rows=[]), sql)[0]["state"] == "empty"
    assert build_exploration(replace(result, rows=result.rows * 2), sql)[0]["charts"] == []
