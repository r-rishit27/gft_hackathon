"""
FastAPI service: natural-language question in, SQL query out.

Wraps the retrieval-augmented text2sql pipeline in text2sql_falkordb.py:
question -> retrieve relevant tables from the FalkorDB knowledge graph ->
serialize schema -> gaussalgo/T5-LM-Large-text2sql-spider -> SQL string.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000

Then:
    POST /generate-sql   {"question": "..."}
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import text2sql_falkordb as pipeline

model_state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the model once at startup instead of per-request.
    tokenizer, model = pipeline.load_model()
    model_state["tokenizer"] = tokenizer
    model_state["model"] = model
    yield
    model_state.clear()


app = FastAPI(
    title="AML Text-to-SQL API",
    description="Generates SQL against the AML knowledge graph schema from a natural-language question.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the standalone frontend (opened via file:// or a local static server)
# to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural-language question about the AML data")
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


class Correction(BaseModel):
    from_: str = Field(..., alias="from")
    to: str

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
            tokenizer=model_state["tokenizer"],
            model=model_state["model"],
        )
    except Exception as exc:  # noqa: BLE001 - surface pipeline errors to the caller
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return QuestionResponse(
        question=outcome["question"],
        sql=outcome["sql"],
        source=outcome["source"],
        matched_question=outcome["matched_question"],
        similarity=outcome["similarity"],
        tables_used=outcome["tables_used"],
        corrections=outcome["corrections"],
        system_prompt=pipeline.SYSTEM_PROMPT if request.with_system_prompt else None,
        model_input=outcome["model_input"],
    )


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": "model" in model_state}


# Serve the chat frontend at /ui (mounted last so it doesn't shadow the API routes above).
app.mount("/ui", StaticFiles(directory="frontend", html=True), name="ui")
