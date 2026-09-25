import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from analytics_service.app import create_app
from analytics_service.bigquery_client import QueryResult
from analytics_service.catalog import load_catalog
from analytics_service.config import Settings
from analytics_service.dashboard import build_dashboard
from analytics_service.demo import FixtureExecutor, FixtureModel, fixture_settings
from analytics_service.metrics import METRICS
from analytics_service.validator import SQLValidator

TOKEN = "test-only-token"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def client():
    with TestClient(create_app(fixture_settings(TOKEN), FixtureModel(), FixtureExecutor())) as client:
        yield client


def test_auth_required_and_user_scope_not_accepted(client):
    assert client.post("/query", json={"question": METRICS[0].question}).status_code == 401
    assert client.get("/metrics").status_code == 401
    response = client.post("/query", headers=HEADERS, json={"question": METRICS[0].question, "scope": "all"})
    assert response.status_code == 422
    assert "test-only-token" not in response.text


@pytest.mark.parametrize("metric", METRICS, ids=lambda m: m.id)
def test_authenticated_question_to_dashboard(client, metric):
    response = client.post("/query", headers=HEADERS, json={"question": metric.question})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["synthetic"] and data["mode"] == "offline_fixture"
    assert data["scope"] == ["HASE_HK"]
    assert data["schema_version"] and data["job_id"]
    assert data["dashboard"]["charts"]
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_unknown_question_and_bad_model_do_not_execute():
    class NeverExecute:
        def execute(self, *args):
            raise AssertionError("must not execute")

    class WrongModel(FixtureModel):
        def generate(self, question):
            return METRICS[1].sql

    with TestClient(create_app(fixture_settings(TOKEN), WrongModel(), NeverExecute())) as client:
        assert client.post("/query", headers=HEADERS, json={"question": "export private emails"}).status_code == 422
        response = client.post("/query", headers=HEADERS, json={"question": METRICS[0].question})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "semantic_mismatch"


def test_per_identity_rate_limit():
    settings = fixture_settings(TOKEN).model_copy(update={"requests_per_minute": 1})
    with TestClient(create_app(settings, FixtureModel(), FixtureExecutor())) as client:
        assert client.post("/query", headers=HEADERS, json={"question": METRICS[4].question}).status_code == 200
        assert client.post("/query", headers=HEADERS, json={"question": METRICS[4].question}).status_code == 429


def test_response_data_and_logs_do_not_include_token_or_prompt(client, caplog):
    with caplog.at_level("INFO", logger="aml.analytics.audit"):
        response = client.post("/query", headers=HEADERS, json={"question": METRICS[4].question})
    assert response.status_code == 200
    assert TOKEN not in caplog.text and METRICS[4].question not in caplog.text
    assert "sql_sha256" in caplog.text


def test_numerical_gold_results():
    scope = fixture_settings(TOKEN).scopes["hk"]
    validator = SQLValidator(load_catalog())
    results = {m.id: FixtureExecutor().execute(validator.validate(m.sql, scope), scope, "test").rows for m in METRICS}
    assert results["transaction_trend"][0]["amount_usd"] == 30.75
    assert sum(r["transaction_count"] for r in results["transaction_trend"]) == 17
    assert results["sar_volume"][0]["sar_cases"] == 1
    assert results["case_backlog"] == [{"open_cases": 2, "closed_cases": 1}]
    assert results["risk_trend"][1]["average_risk_score"] == .6
    assert results["risk_trend"][1]["scored_customers"] == 1
    assert results["customer_count"] == [{"active_customers": 1}]
    assert results["alert_conversion"] == [{"alerted_cases": 2, "sar_cases": 1, "conversion_rate": .5}]


def test_partial_empty_and_null_results():
    result = QueryResult([{"name": "active_customers", "type": "INT64"}], [{"active_customers": None}], "test", 0, False)
    dashboard = build_dashboard(result, METRICS[4])
    assert dashboard["charts"][0]["value"] is None
    assert "not available" in dashboard["insights"][0]
    assert build_dashboard(replace(result, truncated=True), METRICS[4])["charts"] == []
    assert build_dashboard(replace(result, rows=[]), METRICS[4])["state"] == "empty"


def test_scope_routes_to_distinct_views_and_principals():
    settings = fixture_settings(TOKEN).model_dump()
    settings["scopes"]["in"] = {**settings["scopes"]["hk"], "entities": ["HSBC_IN"],
        "service_account": "analytics-in@example-project.iam.gserviceaccount.com",
        "resources": {n: {**r, "view": r["view"].replace("_hk", "_in")} for n, r in settings["scopes"]["hk"]["resources"].items()}}
    settings["identities"].append({"subject": "india-user", "scope": "in", "token_sha256": hashlib.sha256(b"india-token").hexdigest()})
    seen = []

    class RecordingExecutor:
        def execute(self, query, scope, request_id):
            seen.append((query.sql, scope.service_account))
            return QueryResult([], [], "test", 0, False)

    with TestClient(create_app(Settings.model_validate(settings), FixtureModel(), RecordingExecutor())) as client:
        for token in (TOKEN, "india-token"):
            response = client.post("/query", headers={"Authorization": "Bearer " + token}, json={"question": METRICS[4].question})
            assert response.status_code == 200
    assert "aml_analytics_hk" in seen[0][0] and "aml_analytics_in" not in seen[0][0]
    assert "aml_analytics_in" in seen[1][0] and seen[0][1] != seen[1][1]


def test_static_dashboard_and_request_size_limit(client):
    assert client.get("/ui/").status_code == 200
    assert client.get("/ui/vendor/chart.umd.js").status_code == 200
    assert client.post("/query", headers=HEADERS, content="x" * 12001).status_code == 413
