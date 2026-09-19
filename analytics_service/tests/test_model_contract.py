import json

import httpx
import pytest

from analytics_service.errors import AnalyticsError
from analytics_service.model_client import ModelClient


@pytest.mark.parametrize("response", [
    {}, [], {"sql": "SELECT 1", "schema_valid": True},
    {"sql": "SELECT 1", "schema_valid": "true", "schema_violations": []},
    {"sql": "SELECT 1", "schema_valid": False, "schema_violations": []},
    {"sql": "", "schema_valid": True, "schema_violations": []},
    {"sql": "SELECT 1", "schema_valid": True, "schema_violations": ["bad"]},
])
def test_model_fails_closed(response):
    client = ModelClient("http://localhost/generate-sql", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response))))
    with pytest.raises(AnalyticsError):
        client.generate("question")


def test_model_contract_extra_fields_and_timeout():
    requests = []

    def handler(request):
        requests.append(request)
        assert json.loads(request.content) == {"question": "question"}
        return httpx.Response(200, json={"sql": "SELECT 1", "schema_valid": True, "schema_violations": [], "future": 42})

    client = ModelClient("http://localhost/generate-sql", httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.generate("question") == "SELECT 1"
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == 30

    def timeout(request):
        raise httpx.ReadTimeout("secret internal error")

    client = ModelClient("http://localhost", httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(AnalyticsError, match="timed out") as error:
        client.generate("question")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("status,body", [(500, b"secret"), (200, b"not json"), (200, b"x" * 256001)])
def test_model_transport_errors_are_redacted(status, body):
    client = ModelClient("http://localhost", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, content=body))))
    with pytest.raises(AnalyticsError) as exc:
        client.generate("question")
    assert "secret" not in str(exc.value)
