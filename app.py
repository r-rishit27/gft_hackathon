"""
FastAPI service: natural-language question in, SQL query out.

Wraps the retrieval-augmented text2sql pipeline in pipeline/text2sql_falkordb.py:
question -> retrieve relevant tables from the FalkorDB knowledge graph ->
serialize schema as DDL -> mannix/defog-llama3-sqlcoder-8b (via a local
Ollama server) -> SQL string.

This is an internal model-service API, not a public-facing product -- it has
no bundled frontend. analytics_service is the deployed UI that calls it.

Run from the project root:
    uvicorn app:app --host 127.0.0.1 --port 8000

Then:
    POST /generate-sql   {"question": "..."}
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pipeline import text2sql_falkordb as pipeline

app_state = {"ollama_ready": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify the Ollama server is reachable once at startup instead of
    # failing opaquely on the first request. Deliberately does NOT let a
    # failure here crash the whole process: Ollama (reached via a tunnel to
    # a locally-run instance) can be transiently unreachable right at boot
    # -- e.g. mid-restart, or the tunnel's DNS hasn't propagated yet -- and
    # /health already re-checks readiness on every call via the same
    # pipeline.load_model(), so staying up and reporting not-ready is
    # strictly better than crash-looping until the exact moment Ollama
    # happens to be reachable during startup.
    try:
        pipeline.load_model()
        app_state["ollama_ready"] = True
    except Exception as exc:  # noqa: BLE001 - startup readiness probe, not fatal
        print(f"WARNING: Ollama not reachable at startup ({exc}); will retry on /health.")
        app_state["ollama_ready"] = False
    yield
    app_state["ollama_ready"] = False


app = FastAPI(
    title="AML Text-to-SQL API",
    description="Generates SQL against the AML knowledge graph schema from a natural-language question.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow cross-origin callers (analytics_service, or any other deployed
# frontend) to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question about the AML data")
    with_system_prompt: bool = Field(
        False, description="Prepend the business-analyst/read-only-SQL system prompt to the model input"
    )
    top_k: int = Field(6, ge=1, le=20, description="Number of tables the retrieval engine selects before hop expansion")
    use_exemplars: bool = Field(
        True, description="Reuse a verified answer for (near-)duplicate known KPI questions instead of regenerating"
    )
    ground_tables: bool = Field(
        False,
        description="Prepend a dynamic 'use only these exact table names' line. "
        "Evaluated but not measurably better than leaving it off for this model -- opt-in only.",
    )
    retry_on_invalid: bool = Field(
        False,
        description="If the first attempt fails schema validation, widen retrieval and regenerate once "
        "with the violations fed back into the prompt. Evaluated: correctly runs, but did not fix any "
        "case in eval/eval_new_queries.json or eval/eval_heldout.json, while roughly doubling latency "
        "for invalid answers -- opt-in only, may still help on real questions where the true table/"
        "column fell outside the first attempt's retrieved subset.",
    )
    execution_mode: bool = False
    allowed_columns: dict[str, list[str]] | None = None


class Correction(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    kind: str | None = None  # "table" or "column"
    stage: str | None = None  # "kg_repair" (retrieval-scoped) or omitted (canonical schema_validator)

    class Config:
        populate_by_name = True


class QuestionResponse(BaseModel):
    question: str
    sql: str
    source: str  # "exemplar_retrieval" or "model_generation"
    matched_question: str | None
    similarity: float
    tables_used: list[str]
    corrections: list[Correction]
    schema_valid: bool
    schema_violations: list[str]
    retried: bool
    system_prompt: str | None
    model_input: str | None


@app.post("/generate-sql", response_model=QuestionResponse)
def generate_sql(request: QuestionRequest):
    try:
        outcome = pipeline.generate_sql_kag(
            request.question,
            top_k=request.top_k,
            with_system_prompt=request.with_system_prompt,
            use_exemplars=request.use_exemplars,
            ground_tables=request.ground_tables,
            retry_on_invalid=request.retry_on_invalid,
            execution_mode=request.execution_mode,
            allowed_columns=request.allowed_columns,
        )
    except Exception as exc:  # noqa: BLE001 - surface pipeline errors to the caller
        raise HTTPException(status_code=503, detail="Model generation unavailable; check local service logs.") from exc

    return QuestionResponse(
        question=outcome["question"],
        sql=outcome["sql"],
        source=outcome["source"],
        matched_question=outcome["matched_question"],
        similarity=outcome["similarity"],
        tables_used=outcome["tables_used"],
        corrections=outcome["corrections"],
        schema_valid=outcome["schema_valid"],
        schema_violations=outcome["schema_violations"],
        retried=outcome["retried"],
        system_prompt=pipeline.SYSTEM_PROMPT if request.with_system_prompt else None,
        model_input=outcome["model_input"],
    )


@app.get("/health")
def health():
    ready = app_state["ollama_ready"]
    try:
        pipeline.load_model()
        ready = True
    except Exception:
        ready = False
    # generation_ready reflects whether /generate-sql can actually answer right
    # now: Ollama being up, or -- if it's not -- the OpenAI fallback having a
    # key configured, since generate_sql_ollama falls back to it automatically.
    return {"status": "ok", "ollama_ready": ready,
            "generation_ready": ready or bool(pipeline.OPENAI_API_KEY),
            "model": pipeline.OLLAMA_MODEL, "backend": pipeline.MODEL_BACKEND,
            "schema_backend": pipeline.SCHEMA_BACKEND}
