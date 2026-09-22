"""Explicit opt-in only: these tests execute bounded live BigQuery reads."""
import os
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from analytics_service.metrics import METRICS
from analytics_service.bigquery_client import BigQueryExecutor
from analytics_service.catalog import load_catalog
from analytics_service.config import Settings
from analytics_service.validator import SQLValidator

pytestmark = pytest.mark.skipif(os.getenv("AML_RUN_LIVE") != "1", reason="Live model, scoped views and IAM require operator setup")


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.id)
def test_live_question_to_result(metric):
    response = httpx.post(os.environ["AML_LIVE_TEST_URL"].rstrip("/") + "/query",
                          headers={"Authorization": "Bearer " + os.environ["AML_LIVE_TEST_TOKEN"]},
                          json={"question": metric.question}, timeout=310)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "bigquery", "An offline fixture is not a live integration test"
    assert result["job_id"] and result["schema_version"] and result["synthetic"]
    assert result["metric"] == "exploration"
    assert result["semantic_validation"] == "schema_and_policy_only"
    assert not result["truncated"]
    config_path = os.getenv("AML_ANALYTICS_CONFIG", str(Path(__file__).resolve().parents[1] / "config.local.json"))
    settings = Settings.from_file(config_path)
    import hashlib
    digest = hashlib.sha256(os.environ["AML_LIVE_TEST_TOKEN"].encode()).hexdigest()
    identity = next(i for i in settings.identities if i.token_sha256 == digest)
    scope = settings.scopes[identity.scope]
    gold = BigQueryExecutor(settings.maximum_bytes_billed).execute(
        SQLValidator(load_catalog()).validate(metric.sql, scope), scope, str(uuid.uuid4()))
    # The reviewed output column contract makes disagreements visible, including
    # model aliases/shapes requiring review, rather than guessing column meaning.
    names = [c["name"] for c in gold.columns]
    assert {c["name"] for c in result["columns"]} == set(names)
    assert len(result["rows"]) == len(gold.rows)
    expected = sorted(gold.rows, key=lambda r: str([r[n] for n in names]))
    actual = sorted(result["rows"], key=lambda r: str([r[n] for n in names]))
    for left, right in zip(actual, expected):
        for column in gold.columns:
            a, b = left[column["name"]], right[column["name"]]
            if a is not None and b is not None and column["type"] in {"FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}:
                assert float(Decimal(str(a))) == pytest.approx(float(Decimal(str(b))), rel=1e-9)
            else:
                assert a == b
