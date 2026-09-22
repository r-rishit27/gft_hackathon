import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT = "gen-lang-client-0810987953"
DATASET = "aml_demo"
LOCATION = "asia-south1"
ENTITIES = {"HASE_HK", "HSBC_GB", "HSBC_IN", "HSBC_TW", "HSBC_FR", "HSBC_PL", "HSBC_IE"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Resource(StrictModel):
    view: str
    columns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_view(self):
        parts = self.view.split(".")
        if len(parts) != 3 or parts[0] != PROJECT or parts[1] == DATASET:
            raise ValueError("Use an approved view in this project, outside the base dataset")
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts[1:]):
            raise ValueError("Invalid view identifier")
        return self


class Scope(StrictModel):
    entities: list[str] = Field(min_length=1)
    service_account: str
    resources: dict[str, Resource] = Field(min_length=1)

    @model_validator(mode="after")
    def check_scope(self):
        if not set(self.entities) <= ENTITIES:
            raise ValueError("Unknown entity-country scope")
        if not re.fullmatch(r"[a-z][a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com", self.service_account):
            raise ValueError("A scoped execution service account is required")
        return self


class Identity(StrictModel):
    subject: str = Field(min_length=1, max_length=100)
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: str


class Settings(StrictModel):
    model_url: str = "http://127.0.0.1:8000/generate-sql"
    maximum_bytes_billed: int = Field(gt=0)
    scopes: dict[str, Scope] = Field(min_length=1)
    identities: list[Identity] = Field(min_length=1)
    requests_per_minute: int = Field(default=10, ge=1, le=60)
    max_concurrent_queries: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def validate_settings(self):
        from urllib.parse import urlparse
        url = urlparse(self.model_url)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Model URL must not contain credentials, query or fragment")
        if not url.hostname or (url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
        )):
            raise ValueError("Model URL must use HTTPS or loopback HTTP")
        if any(i.scope not in self.scopes for i in self.identities):
            raise ValueError("Identity references an unknown scope")
        if len({i.token_sha256 for i in self.identities}) != len(self.identities):
            raise ValueError("Each identity must have a distinct token")
        if len({i.subject for i in self.identities}) != len(self.identities):
            raise ValueError("Each subject must be unique")
        principals = [s.service_account for s in self.scopes.values()]
        if len(set(principals)) != len(principals):
            raise ValueError("Each scope requires its own execution principal")
        return self

    @classmethod
    def from_file(cls, path: str):
        return cls.model_validate(json.loads(Path(path).read_text()))
