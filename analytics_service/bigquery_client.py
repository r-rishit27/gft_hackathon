import datetime as dt
import decimal
import json
import math
import time
from dataclasses import dataclass

import google.auth
from google.api_core.exceptions import BadRequest
from google.auth import impersonated_credentials
from google.cloud import bigquery

from .config import LOCATION, PROJECT, Scope
from .errors import AnalyticsError
from .validator import ValidatedQuery

ROW_LIMIT = 1000
RESULT_BYTES_LIMIT = 2_000_000


def json_value(value):
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)  # Do not lose NUMERIC precision through binary floats.
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (dict, bigquery.Row)):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


@dataclass
class QueryResult:
    columns: list[dict]
    rows: list[dict]
    job_id: str
    bytes_processed: int
    truncated: bool
    mode: str = "bigquery"


def scoped_client(scope: Scope):
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    target = impersonated_credentials.Credentials(
        source_credentials=credentials, target_principal=scope.service_account,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"], lifetime=900,
    )
    return bigquery.Client(project=PROJECT, credentials=target, location=LOCATION)


class BigQueryExecutor:
    def __init__(self, maximum_bytes_billed: int, client_factory=scoped_client, clock=time.monotonic):
        if maximum_bytes_billed <= 0:
            raise ValueError("A positive scan budget is mandatory")
        self.maximum_bytes_billed = maximum_bytes_billed
        self.client_factory = client_factory
        self.clock = clock

    def status(self, scope):
        client = None
        try:
            client = self.client_factory(scope)
            checked = []
            for resource in scope.resources.values():
                table = client.get_table(resource.view, timeout=5, retry=None)
                if table.table_type != "VIEW" or table.location != LOCATION:
                    return {"ready": False, "message": "An approved view is absent or in the wrong region."}
                checked.append(resource.view)
            return {"ready": True, "approved_views": len(checked)}
        except Exception:
            return {"ready": False, "message": "BigQuery authentication or approved-view access is not configured."}
        finally:
            if client is not None:
                client.close()

    def execute(self, query: ValidatedQuery, scope: Scope, request_id: str) -> QueryResult:
        deadline = self.clock() + 60
        client = self.client_factory(scope)
        execution_id = "aml_" + request_id.replace("-", "")
        submitted = False

        def remaining():
            seconds = deadline - self.clock()
            if seconds <= 0:
                raise TimeoutError()
            return seconds

        def cancel():
            try:
                return client.cancel_job(execution_id, project=PROJECT, location=LOCATION, retry=None, timeout=5)
            except Exception:
                return False

        try:
            for resource in scope.resources.values():
                view = client.get_table(resource.view, retry=None, timeout=remaining())
                if view.table_type != "VIEW" or view.location != LOCATION:
                    raise AnalyticsError("configuration", "An approved view is absent or in the wrong region.", 503)
            dry_config = bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, use_legacy_sql=False,
                maximum_bytes_billed=self.maximum_bytes_billed,
            )
            dry = client.query(query.sql, job_config=dry_config, location=LOCATION,
                               retry=None, job_retry=None, timeout=remaining())
            estimated = dry.total_bytes_processed
            if estimated is None or estimated > self.maximum_bytes_billed:
                raise AnalyticsError("scan_budget", "The query exceeds the configured scan budget or has no estimate.")
            config = bigquery.QueryJobConfig(
                use_legacy_sql=False, use_query_cache=False,
                maximum_bytes_billed=self.maximum_bytes_billed,
                job_timeout_ms=max(1, int(remaining() * 1000)),
                labels={"application": "aml-analytics", "request": request_id.replace("-", "")},
            )
            # Reserve a known ID before submission, allowing cancellation after ambiguous network failures.
            submitted = True
            job = client.query(query.sql, job_config=config, location=LOCATION, job_id=execution_id,
                               retry=None, job_retry=None, timeout=remaining())
            rows = job.result(timeout=remaining(), max_results=ROW_LIMIT + 1, page_size=ROW_LIMIT + 1,
                              retry=None, job_retry=None)
            output = []
            size = 0
            truncated = bool(rows.total_rows is not None and rows.total_rows > ROW_LIMIT)
            for index, row in enumerate(rows):
                remaining()
                if index >= ROW_LIMIT:
                    truncated = True
                    break
                converted = json_value(dict(row.items()))
                size += len(json.dumps(converted, allow_nan=False).encode())
                if size > RESULT_BYTES_LIMIT:
                    truncated = True
                    break
                output.append(converted)
            return QueryResult(
                columns=[{"name": f.name, "type": f.field_type, "mode": f.mode} for f in rows.schema],
                rows=output, job_id=job.job_id, bytes_processed=job.total_bytes_processed or 0,
                truncated=truncated,
            )
        except AnalyticsError:
            raise
        except BadRequest:
            if submitted:
                cancel()
            raise AnalyticsError("invalid_google_sql", "BigQuery rejected the generated SQL. Try a more specific question; no results were returned.", 422) from None
        except TimeoutError:
            cancelled = cancel() if submitted else True
            message = "The query deadline was reached; cancellation was requested."
            if not cancelled:
                message += " Cancellation could not be confirmed; an operator must check the request job ID."
            raise AnalyticsError("query_timeout", message, 504) from None
        except Exception:
            cancelled = cancel() if submitted else True
            message = "BigQuery could not complete this request."
            if not cancelled:
                message += " Cancellation could not be confirmed; an operator must check the request job ID."
            raise AnalyticsError("bigquery_unavailable", message, 503) from None
        finally:
            client.close()
