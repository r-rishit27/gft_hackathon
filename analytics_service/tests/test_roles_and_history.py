import time

from fastapi.testclient import TestClient

from analytics_service.app import create_app
from analytics_service.bigquery_client import QueryResult
from analytics_service.config import Identity, Settings
from analytics_service.demo import fixture_settings
from analytics_service.password_auth import PasswordAuth, hash_password

HEADERS = {"X-AML-Request": "1"}


def settings(tmp_path):
    base = fixture_settings("disabled-old-token").model_dump()
    source = base["scopes"]["hk"]
    base.update(query_mode="freeform", history_path=str(tmp_path / "history.sqlite"), identities=[], scopes={})
    for role, tables in [("monitoring", ["Party", "Transaction"]), ("investigation", ["Party", "RiskCaseEvent"]), ("admin", ["Party", "Transaction", "RiskCaseEvent"])]:
        base["scopes"][role] = {**source, "service_account": f"{role}@example-project.iam.gserviceaccount.com",
                                "resources": {t: source["resources"][t] for t in tables}}
        base["identities"].append({"subject": role, "username": role, "password_hash": hash_password("test-pass-123"), "scope": role})
    return Settings.model_validate(base)


class Model:
    def __init__(self):
        self.allowed = []

    def generate(self, question, allowed_columns):
        self.allowed.append(set(allowed_columns))
        # Deliberately untrusted model: the independent gate must catch these.
        return "SELECT COUNT(*) AS n FROM " + ("Transaction" if "transaction" in question else "RiskCaseEvent")

    def status(self):
        return {"ready": True}

    def close(self):
        pass


class Executor:
    def __init__(self):
        self.principals = []

    def execute(self, query, scope, request_id):
        self.principals.append(scope.service_account)
        return QueryResult([{"name": "n", "type": "INTEGER"}], [{"n": 1}], "test", 0, False)

    def status(self, scope):
        return {"ready": True}


def login(client, role):
    return client.post("/auth/login", json={"username": role, "password": "test-pass-123"}, headers=HEADERS)


def test_login_cookie_csrf_logout_and_no_role_spoof(tmp_path):
    with TestClient(create_app(settings(tmp_path), Model(), Executor())) as client:
        assert client.post("/auth/login", json={"username": "monitoring", "password": "test-pass-123"}).status_code == 403
        assert client.post("/auth/login", headers=HEADERS, json={"username": "monitoring", "password": "wrong"}).status_code == 401
        response = login(client, "monitoring")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=3600" in cookie
        assert "password" not in response.text and "token" not in response.text
        assert client.get("/auth/me").json()["role"] == "monitoring"
        assert client.post("/query", json={"question": "transaction count"}).status_code == 403
        assert client.post("/query", headers=HEADERS, json={"question": "transaction count", "scope": "investigation"}).status_code == 422
        assert client.post("/auth/logout", headers=HEADERS).status_code == 200
        assert client.get("/workspace").status_code == 401
        assert client.get("/status", headers={"Authorization": "Bearer disabled-old-token"}).status_code == 401


def test_permissions_enforced_before_execution_and_history_isolation(tmp_path):
    model, executor = Model(), Executor()
    with TestClient(create_app(settings(tmp_path), model, executor)) as client:
        assert login(client, "monitoring").status_code == 200
        for _ in range(2):
            assert client.post("/query", headers=HEADERS, json={"question": "transaction count"}).status_code == 200
        denied = client.post("/query", headers=HEADERS, json={"question": "case count"})
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "resource_denied"
        assert "You do not have the required access" in denied.json()["error"]["message"]
        assert len(model.allowed) == 2
        assert len(executor.principals) == 2
        assert model.allowed[-1] == {"Party", "Transaction"}
        assert client.post("/workspace/saved", headers=HEADERS, json={"question": "transaction count"}).status_code == 200
        workspace = client.get("/workspace").json()
        assert len(workspace["history"]) == 3 and workspace["frequent"][0]["count"] == 2
        assert workspace["saved"] == ["transaction count"]
        assert login(client, "investigation").status_code == 200
        assert client.get("/workspace").json() == {"saved": [], "history": [], "frequent": []}
        assert client.post("/query", headers=HEADERS, json={"question": "transaction count"}).status_code == 403
        assert client.post("/query", headers=HEADERS, json={"question": "case count"}).status_code == 200
        assert "investigation@" in executor.principals[-1]
        assert model.allowed[-1] == {"Party", "RiskCaseEvent"}
        assert login(client, "monitoring").status_code == 200
        assert client.delete("/workspace/history", headers=HEADERS).status_code == 200
        workspace = client.get("/workspace").json()
        assert workspace["history"] == [] and workspace["frequent"] == []
        assert workspace["saved"] == ["transaction count"]


def test_separate_login_profile_and_admin_union(tmp_path):
    model, executor = Model(), Executor()
    with TestClient(create_app(settings(tmp_path), model, executor)) as client:
        for path in ["/ui/", "/profile"]:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/login"
        assert 'id="password"' in client.get("/login").text
        assert login(client, "admin").status_code == 200
        assert client.get("/login", follow_redirects=False).headers["location"] == "/ui/"
        page = client.get("/ui/").text
        assert 'id="password"' not in page and 'id="sign-in"' not in page
        assert 'href="/profile"' in page
        assert 'id="profile-countries"' in client.get("/profile").text
        profile = client.get("/auth/me").json()
        assert profile["tables"] == ["Party", "RiskCaseEvent", "Transaction"]
        assert profile["countries"] == [{"code": "HK", "name": "Hong Kong", "entity": "HASE"}]
        assert profile["read_only"] is True
        for question in ["transaction count", "case count"]:
            assert client.post("/query", headers=HEADERS, json={"question": question}).status_code == 200
        assert len(executor.principals) == 2
        assert all("admin@" in principal for principal in executor.principals)
        denied = client.post("/query", headers=HEADERS, json={"question": "Show ExportedMetadata"})
        assert denied.status_code == 403


def test_allowed_question_with_forbidden_model_sql_never_executes(tmp_path):
    class UntrustedModel(Model):
        def generate(self, question, allowed_columns):
            return "SELECT COUNT(*) AS n FROM RiskCaseEvent"

    executor = Executor()
    with TestClient(create_app(settings(tmp_path), UntrustedModel(), executor)) as client:
        assert login(client, "monitoring").status_code == 200
        denied = client.post("/query", headers=HEADERS, json={"question": "transaction count"})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "resource_denied"
        assert "You do not have the required access" in denied.json()["error"]["message"]
        assert executor.principals == []


def test_session_expiry_rotation_and_login_rate_limit():
    identity = Identity(subject="user", scope="monitoring", username="monitoring", password_hash=hash_password("test-pass-123"))
    auth = PasswordAuth([identity])
    first, _ = auth.login("monitoring", "test-pass-123")
    second, _ = auth.login("monitoring", "test-pass-123", first)
    assert auth.authenticate(first) is None and auth.authenticate(second) is identity
    auth.sessions = {k: (v[0], time.monotonic() - 1) for k, v in auth.sessions.items()}
    assert auth.authenticate(second) is None
    auth.attempts.extend([time.monotonic()] * 10)
    import pytest
    from analytics_service.errors import AnalyticsError
    with pytest.raises(AnalyticsError, match="Too many"):
        auth.login("different-name", "wrong")
