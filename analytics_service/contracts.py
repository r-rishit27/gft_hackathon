from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)


class Column(BaseModel):
    name: str
    type: str
    mode: str = "NULLABLE"


class Dashboard(BaseModel):
    state: Literal["empty", "partial", "complete"]
    charts: list[dict[str, Any]]
    insights: list[str]


class QueryResponse(BaseModel):
    request_id: str
    job_id: str
    metric: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    sql: str
    schema_version: str
    scope: list[str]
    units: dict[str, str]
    data_as_of: str
    timezone: str
    synthetic: Literal[True]
    mode: Literal["bigquery", "offline_fixture"]
    bytes_processed: int
    truncated: bool
    warnings: list[str]
    dashboard: Dashboard
