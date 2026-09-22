"""Explicit opt-in only: these tests execute bounded live BigQuery reads."""
import os

import httpx
import pytest

from analytics_service.metrics import METRICS

pytestmark = pytest.mark.skipif(os.getenv("AML_RUN_LIVE") != "1", reason="Live model, scoped views and IAM require operator setup")


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.id)
def test_live_question_to_result(metric):
    response = httpx.post(os.environ["AML_LIVE_TEST_URL"].rstrip("/") + "/query",
                          headers={"Authorization": "Bearer " + os.environ["AML_LIVE_TEST_TOKEN"]},
                          json={"question": metric.question}, timeout=100)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "bigquery", "An offline fixture is not a live integration test"
    assert result["job_id"] and result["schema_version"] and result["synthetic"]
    assert result["metric"] == metric.id
