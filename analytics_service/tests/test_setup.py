from analytics_service.setup_local import grant, signature


def test_recursive_schema_type_aliases():
    a = [{"name": "amount", "type": "RECORD", "fields": [{"name": "units", "type": "INTEGER"}]}]
    b = [{"name": "amount", "type": "STRUCT", "mode": "NULLABLE", "fields": [{"name": "units", "type": "INT64", "mode": "NULLABLE"}]}]
    assert signature(a) == signature(b)
    b[0]["fields"][0]["mode"] = "REQUIRED"
    assert signature(a) != signature(b)


def test_iam_grant_preserves_conditions_etag_and_unrelated_bindings():
    policy = {"version": 3, "etag": "original", "bindings": [
        {"role": "roles/viewer", "members": ["user:someone@example.com"]},
        {"role": "roles/bigquery.jobUser", "members": ["user:conditional@example.com"],
         "condition": {"title": "existing", "expression": "true"}},
    ]}
    calls = []

    class Response:
        ok = True
        content = b"json"

        def json(self):
            return policy

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return Response()

    grant(Session(), "https://example.test/project", "roles/bigquery.jobUser", "serviceAccount:scoped@example.com")
    assert calls[0][1] == {"options": {"requestedPolicyVersion": 3}}
    saved = calls[1][1]["policy"]
    assert saved["etag"] == "original" and saved["version"] == 3
    assert saved["bindings"][0]["members"] == ["user:someone@example.com"]
    assert saved["bindings"][1]["condition"]["expression"] == "true"
    assert saved["bindings"][2]["members"] == ["serviceAccount:scoped@example.com"]
