import json

import httpx

from .errors import AnalyticsError
from .access_intent import ACCESS_MESSAGE, denied_sql_tables
from .catalog import load_catalog


class ModelClient:
    def __init__(self, url: str, client=None, timeout=30):
        self.url = url
        self.timeout = timeout
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def generate(self, question: str, allowed_columns=None) -> str:
        payload = {"question": question}
        if allowed_columns is not None:
            payload.update(execution_mode=True, use_exemplars=False, with_system_prompt=True,
                           allowed_columns=allowed_columns)
        try:
            with self.client.stream("POST", self.url, json=payload, timeout=self.timeout) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 256_000:
                        raise AnalyticsError("model_response", "Model response exceeds the size limit.", 502)
                data = json.loads(body)
        except httpx.TimeoutException:
            raise AnalyticsError("model_timeout", "The model service timed out; nothing was executed.", 504) from None
        except (httpx.HTTPError, ValueError):
            raise AnalyticsError("model_unavailable", "The model service returned an unusable response.", 502) from None
        if isinstance(data, dict) and allowed_columns is not None and denied_sql_tables(data.get("sql"), allowed_columns, load_catalog()["tables"]):
            raise AnalyticsError("resource_denied", ACCESS_MESSAGE, 403)
        if isinstance(data, dict) and data.get("schema_violations") == ["clarification_required"]:
            raise AnalyticsError("clarification_required", "Please clarify the measure, country scope or time period. The model could not safely answer this question from the available schema.", 422)
        if not isinstance(data, dict) or data.get("schema_valid") is not True:
            raise AnalyticsError("model_rejected", "The model did not validate its candidate query.", 422)
        if data.get("schema_violations") != []:
            raise AnalyticsError("model_rejected", "The model reported schema violations or an incomplete response.", 422)
        sql = data.get("sql")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 32_000:
            raise AnalyticsError("model_response", "The model returned missing or invalid SQL.", 502)
        return sql

    def status(self):
        try:
            response = self.client.get(self.url.rsplit("/", 1)[0] + "/health", timeout=5)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Malformed model health response")
            # generation_ready covers the OpenAI fallback too; fall back to the
            # older ollama_ready-only field for a model service that hasn't
            # picked up that response field yet.
            ready_field = data.get("generation_ready", data.get("ollama_ready"))
            return {"ready": response.status_code == 200 and ready_field is True,
                    "model": data.get("model", "unknown"), "backend": data.get("backend", "unknown"),
                    "schema_backend": data.get("schema_backend", "unknown")}
        except (httpx.HTTPError, ValueError):
            return {"ready": False, "model": "mannix/defog-llama3-sqlcoder-8b", "backend": "ollama"}

    def close(self):
        self.client.close()
