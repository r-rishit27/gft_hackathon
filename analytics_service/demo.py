"""Explicit offline fixture mode. No network calls or cloud credentials."""
import hashlib
import os
from pathlib import Path

import sqlglot
from sqlglot import exp

from .app import create_app
from .bigquery_client import QueryResult, json_value
from .config import Settings
from .metrics import resolve_metric

DEMO_COLUMNS = {
    "Party": ["party_id", "validity_start_time", "is_entity_deleted", "exit_date", "join_date"],
    "Transaction": ["book_time", "direction", "is_entity_deleted", "normalized_booked_amount"],
    "RiskCaseEvent": ["risk_case_id", "type", "event_time"],
    "RiskScores": ["party_id", "risk_period_end_time", "risk_score"],
}


def fixture_settings(token: str):
    return Settings.model_validate({
        "query_mode": "reviewed",
        "maximum_bytes_billed": 10_000_000,
        "identities": [{"subject": "local-demo", "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "scope": "hk"}],
        "scopes": {"hk": {
            "entities": ["HASE_HK"], "service_account": "analytics-hk@example-project.iam.gserviceaccount.com",
            "resources": {name: {"view": "gen-lang-client-0810987953.aml_analytics_hk." + name,
                                 "columns": columns} for name, columns in DEMO_COLUMNS.items()},
        }},
    })


class FixtureModel:
    def generate(self, question):
        return resolve_metric(question).sql

    def close(self):
        pass


class FixtureExecutor:
    mode = "offline_fixture"

    def execute(self, query, scope, request_id):
        import duckdb
        with duckdb.connect(":memory:") as db:
            db.execute((Path(__file__).parent / "fixtures.sql").read_text())
            tree = sqlglot.parse_one(query.sql, read="bigquery")
            for table in tree.find_all(exp.Table):
                if table.db:
                    table.set("catalog", None)
                    table.set("db", None)
            cursor = db.execute(tree.sql("duckdb"))
            columns = [{"name": d[0], "type": str(d[1]), "mode": "NULLABLE"} for d in cursor.description]
            rows = [dict(zip([c["name"] for c in columns], [json_value(v) for v in row])) for row in cursor.fetchall()]
        return QueryResult(columns, rows, "fixture_" + request_id, 0, False, "offline_fixture")


def create_demo_app():
    token = os.environ.get("AML_DEMO_TOKEN", "")
    if len(token) < 24:
        raise ValueError("Set AML_DEMO_TOKEN to at least 24 characters; bind this demo to loopback only")
    return create_app(fixture_settings(token), FixtureModel(), FixtureExecutor())
