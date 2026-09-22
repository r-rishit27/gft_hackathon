import json

import httpx

from .errors import AnalyticsError


class ModelClient:
    def __init__(self, url: str, client=None):
        self.url = url
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def generate(self, question: str) -> str:
        try:
            with self.client.stream("POST", self.url, json={"question": question}, timeout=30) as response:
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
        if not isinstance(data, dict) or data.get("schema_valid") is not True:
            raise AnalyticsError("model_rejected", "The model did not validate its candidate query.", 422)
        if data.get("schema_violations") != []:
            raise AnalyticsError("model_rejected", "The model reported schema violations or an incomplete response.", 422)
        sql = data.get("sql")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 32_000:
            raise AnalyticsError("model_response", "The model returned missing or invalid SQL.", 502)
        return sql

    def close(self):
        self.client.close()
