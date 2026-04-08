"""api/main.py

FastAPI service exposing the /ask endpoint for the GDrive RAG Assistant.
Deployed as a Cloud Run service.

Endpoints:
  POST /ask          - answer a question with cited chunks
  GET  /health       - liveness probe
  GET  /ready        - readiness probe (checks BQ connectivity)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.retriever import Retriever
from api.generator import Generator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="GDrive RAG Assistant",
    description="RAG over Google Drive + GCS via Vertex AI embeddings and Gemini Flash",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to your UI domain in production
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Singleton services (initialised once at startup)
# ---------------------------------------------------------------------------

retriever: Retriever | None = None
generator: Generator | None = None


@app.on_event("startup")
async def startup_event() -> None:
    """Initialise heavyweight clients once when the container starts."""
    global retriever, generator
    logger.info("Initialising Retriever and Generator...")
    retriever = Retriever()   # loads BQ client, embedding model
    generator = Generator()   # loads Gemini client
    logger.info("Ready.")


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000,
                          description="Natural-language question")
    top_k: int = Field(default=8, ge=1, le=20,
                       description="Number of chunks to retrieve")
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    session_id: Optional[str] = Field(default=None,
                                       description="Optional session ID for multi-turn context")


class CitedChunk(BaseModel):
    chunk_id: str
    file_id: str
    file_name: str
    section: str
    score: float
    text_snippet: str


class AskResponse(BaseModel):
    answer: str
    citations: list[CitedChunk]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/ask", response_model=AskResponse, summary="Answer a question with citations")
async def ask(req: AskRequest, request: Request) -> AskResponse:
    """Retrieve relevant chunks from BigQuery and generate a cited answer via Gemini Flash.

    Flow:
      1. Embed the question with text-embedding-004
      2. ANN search in BigQuery VECTOR_INDEX -> top_k chunks
      3. Re-rank chunks (cross-encoder)
      4. Assemble context with [source] tags
      5. Call Gemini 2.0 Flash with token-budget enforcement
      6. Return answer + structured citations
    """
    if retriever is None or generator is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Service not ready")

    t0 = time.perf_counter()

    # Step 1 & 2: embed + retrieve
    try:
        chunks = await retriever.retrieve(
            question=req.question,
            top_k=req.top_k,
        )
    except Exception as exc:
        logger.error("Retrieval failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Retrieval error") from exc

    if not chunks:
        return AskResponse(
            answer="I could not find relevant information in the knowledge base.",
            citations=[],
            model="gemini-2.0-flash-001",
            input_tokens=0,
            output_tokens=0,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    # Step 3-5: generate
    try:
        gen_result = await generator.generate(
            question=req.question,
            chunks=chunks,
            temperature=req.temperature,
        )
    except Exception as exc:
        logger.error("Generation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Generation error") from exc

    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.info(
        "ask | q=%r chunks=%d tokens_in=%d tokens_out=%d latency_ms=%s",
        req.question[:80], len(chunks),
        gen_result.input_tokens, gen_result.output_tokens, latency_ms,
    )

    return AskResponse(
        answer=gen_result.answer,
        citations=[
            CitedChunk(
                chunk_id=c.chunk_id,
                file_id=c.file_id,
                file_name=c.file_name,
                section=c.section,
                score=c.score,
                text_snippet=c.text[:200],
            )
            for c in chunks[:gen_result.cited_chunk_count]
        ],
        model=gen_result.model,
        input_tokens=gen_result.input_tokens,
        output_tokens=gen_result.output_tokens,
        latency_ms=latency_ms,
    )


@app.get("/health", summary="Liveness probe")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/ready", summary="Readiness probe")
async def ready() -> dict:
    """Check that the BigQuery client can list datasets (light connectivity check)."""
    if retriever is None:
        raise HTTPException(status_code=503, detail="Retriever not initialised")
    # TODO: add retriever.ping() method that does a cheap BQ dry-run query
    return {"status": "ready"}


# ---------------------------------------------------------------------------
# Local dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8080, reload=True)
