import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.cloud import bigquery

from analytics_service.bigquery_client import BigQueryExecutor, json_value
from analytics_service.demo import fixture_settings
from analytics_service.errors import AnalyticsError
from analytics_service.validator import ValidatedQuery


class Rows(list):
    total_rows = 1
    schema = [bigquery.SchemaField("amount", "NUMERIC")]


class Client:
    def __init__(self, estimate=100, timeout=False, count=1):
        self.estimate, self.timeout, self.count = estimate, timeout, count
        self.calls, self.cancelled, self.closed = [], False, False

    def get_table(self, name, **kwargs):
        return SimpleNamespace(table_type="VIEW", location="asia-south1")

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        if kwargs["job_config"].dry_run:
            return SimpleNamespace(total_bytes_processed=self.estimate)
        return SimpleNamespace(result=self.result, job_id=kwargs["job_id"], total_bytes_processed=100)

    def result(self, **kwargs):
        assert kwargs["max_results"] == 1001
        assert 0 < kwargs["timeout"] <= 60
        if self.timeout:
            raise TimeoutError("sensitive backend detail")
        rows = Rows({"amount": Decimal("10.500000001")} for _ in range(min(self.count, 1001)))
        rows.total_rows = self.count
        return rows

    def cancel_job(self, job_id, **kwargs):
        self.cancelled = True
        assert kwargs["location"] == "asia-south1"
        return True

    def close(self):
        self.closed = True


def execute(client):
    return BigQueryExecutor(1000, lambda scope: client).execute(
        ValidatedQuery("SELECT 1", "hash"), fixture_settings("token").scopes["hk"], "request-id"
    )


def test_identical_dry_run_and_execution_with_budget_and_region():
    client = Client()
    result = execute(client)
    assert len(client.calls) == 2
    assert client.calls[0][0] == client.calls[1][0]
    for _, kwargs in client.calls:
        assert kwargs["job_config"].maximum_bytes_billed == 1000
        assert kwargs["job_config"].use_legacy_sql is False
        assert kwargs["location"] == "asia-south1"
        assert kwargs["retry"] is None and kwargs["job_retry"] is None
    assert 0 < int(client.calls[1][1]["job_config"].job_timeout_ms) <= 60_000
    assert result.rows == [{"amount": "10.500000001"}]
    assert client.closed


@pytest.mark.parametrize("estimate", [1001, None])
def test_budget_rejection_never_executes(estimate):
    client = Client(estimate=estimate)
    with pytest.raises(AnalyticsError) as exc:
        execute(client)
    assert exc.value.code == "scan_budget"
    assert len(client.calls) == 1
    assert client.closed


def test_timeout_cancels_job():
    client = Client(timeout=True)
    with pytest.raises(AnalyticsError) as exc:
        execute(client)
    assert exc.value.code == "query_timeout"
    assert client.cancelled and client.closed
    assert "sensitive" not in str(exc.value)


def test_ambiguous_submission_is_cancelled_by_known_id():
    client = Client()
    original = client.query

    def query(sql, **kwargs):
        if not kwargs["job_config"].dry_run:
            raise ConnectionError("secret endpoint")
        return original(sql, **kwargs)

    client.query = query
    with pytest.raises(AnalyticsError):
        execute(client)
    assert client.cancelled


def test_unconfirmed_cancellation_is_not_reported_as_success():
    client = Client(timeout=True)
    client.cancel_job = lambda *a, **kw: False
    with pytest.raises(AnalyticsError, match="could not be confirmed"):
        execute(client)


def test_region_and_resource_type_are_checked():
    for view in (SimpleNamespace(table_type="TABLE", location="asia-south1"),
                 SimpleNamespace(table_type="VIEW", location="US")):
        client = Client()
        client.get_table = lambda *a, **kw: view
        with pytest.raises(AnalyticsError) as exc:
            execute(client)
        assert exc.value.code == "configuration"
        assert not client.calls


@pytest.mark.parametrize("count,truncated", [(0, False), (1000, False), (1001, True), (5000, True)])
def test_row_limit_and_truncation(count, truncated):
    result = execute(Client(count=count))
    assert len(result.rows) == min(count, 1000)
    assert result.truncated == truncated


def test_json_numeric_and_timestamp_safety():
    assert json_value(2**60) == str(2**60)
    assert json_value(float("nan")) is None
    assert json_value(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).endswith("+00:00")


def test_response_byte_limit():
    client = Client()
    rows = Rows([{"amount": "x" * 2_000_001}])
    client.result = lambda **kwargs: rows
    result = execute(client)
    assert result.rows == [] and result.truncated
