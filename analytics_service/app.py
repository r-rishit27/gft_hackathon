import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .bigquery_client import BigQueryExecutor
from .catalog import load_catalog
from .config import Identity, Settings
from .contracts import Question, QueryResponse
from .dashboard import build_dashboard
from .errors import AnalyticsError
from .metrics import METRICS, resolve_metric
from .model_client import ModelClient
from .validator import SQLValidator

LOG = logging.getLogger("aml.analytics.audit")
BEARER = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None, model=None, executor=None):
    if not LOG.handlers:
        LOG.addHandler(logging.StreamHandler())
    LOG.setLevel(logging.INFO)
    settings = settings or Settings.from_file(os.environ["AML_ANALYTICS_CONFIG"])
    catalog = load_catalog()
    validator = SQLValidator(catalog)
    model = model or ModelClient(settings.model_url)
    executor = executor or BigQueryExecutor(settings.maximum_bytes_billed)
    semaphore = threading.BoundedSemaphore(settings.max_concurrent_queries)
    rate_lock = threading.Lock()
    requests = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        yield
        model.close()

    app = FastAPI(title="AML Analytics", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        request.state.request_id = str(uuid.uuid4())
        # Bound the body before the framework parses JSON, including chunked requests.
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 12_000:
                    return JSONResponse({"error": {"code": "request_too_large", "message": "Request is too large."}}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    @app.exception_handler(AnalyticsError)
    async def handled_error(request, exc):
        LOG.info(json.dumps({"request_id": request.state.request_id, "decision": "rejected", "code": exc.code,
                             "subject": getattr(request.state, "subject", None),
                             "scope": getattr(request.state, "scope", None)}))
        return JSONResponse({"request_id": request.state.request_id,
                             "error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"request_id": request.state.request_id,
                             "error": {"code": "invalid_request", "message": "Supply only a question of 1-2000 characters."}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        LOG.error(json.dumps({"request_id": request.state.request_id, "decision": "failed", "code": "internal_error"}))
        return JSONResponse({"request_id": request.state.request_id,
                             "error": {"code": "internal_error", "message": "Request could not be completed."}}, status_code=500)

    def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)) -> Identity:
        if not credentials or len(credentials.credentials) > 512:
            raise AnalyticsError("unauthorized", "A valid access token is required.", 401)
        digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
        for identity in settings.identities:
            if hmac.compare_digest(digest, identity.token_sha256):
                request.state.subject = identity.subject
                request.state.scope = identity.scope
                return identity
        raise AnalyticsError("unauthorized", "A valid access token is required.", 401)

    @app.get("/health")
    def health():
        return {"status": "ok", "checks": "process only; not model or BigQuery readiness"}

    @app.get("/metrics")
    def metrics(identity: Identity = Depends(authenticate)):
        scope = settings.scopes[identity.scope]
        return {"scope": scope.entities, "schema_version": catalog["version"],
                "mode": getattr(executor, "mode", "bigquery"),
                "questions": [{"id": m.id, "question": m.question, "units": m.units} for m in METRICS]}

    @app.post("/query", response_model=QueryResponse)
    def query(body: Question, request: Request, identity: Identity = Depends(authenticate)):
        started = time.monotonic()
        with rate_lock:
            recent = requests[identity.subject]
            while recent and recent[0] < started - 60:
                recent.popleft()
            if len(recent) >= settings.requests_per_minute:
                raise AnalyticsError("rate_limit", "Request limit reached. Try again later.", 429)
            recent.append(started)
        if not semaphore.acquire(blocking=False):
            raise AnalyticsError("busy", "Query capacity is busy. Try again later.", 429)
        try:
            metric = resolve_metric(body.question)
            scope = settings.scopes[identity.scope]
            # Check scope support before contacting the model. Only a canonical, non-sensitive question is sent.
            validator.validate(metric.sql, scope)
            candidate = model.generate(metric.question)
            approved = validator.validate_metric(candidate, metric.sql, scope)
            request_id = request.state.request_id
            result = executor.execute(approved, scope, request_id)
            warnings = ["Synthetic demo data; risk outputs are simulated.",
                        "KPI definitions require domain-owner approval before real banking use."]
            if result.mode != "bigquery":
                warnings.append("OFFLINE FIXTURE: neither the live model nor BigQuery was called.")
            if result.truncated:
                warnings.append("Result limit reached; the table is incomplete.")
            response = {
                "request_id": request_id, "job_id": result.job_id, "metric": metric.id,
                "columns": result.columns, "rows": result.rows, "sql": approved.sql,
                "schema_version": catalog["version"], "scope": scope.entities, "units": metric.units,
                "data_as_of": "2026-08-31T23:59:59Z", "timezone": "UTC", "synthetic": True,
                "mode": result.mode, "bytes_processed": result.bytes_processed,
                "truncated": result.truncated, "warnings": warnings,
                "dashboard": build_dashboard(result, metric),
            }
            LOG.info(json.dumps({"request_id": request_id, "subject": identity.subject, "scope": identity.scope,
                                 "schema_version": catalog["version"], "sql_sha256": approved.sha256,
                                 "job_id": result.job_id, "bytes_processed": result.bytes_processed,
                                 "latency_ms": round((time.monotonic() - started) * 1000), "decision": "completed"}))
            return response
        finally:
            semaphore.release()

    app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "ui", html=True), name="ui")
    return app
