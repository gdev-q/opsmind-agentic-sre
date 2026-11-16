"""FastAPI app: /health, /ingest, /triage."""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from app import __version__
from app.agents import get_graph, run_triage
from app.config import ProviderError, get_settings
from app.rag_pipeline import RunbookStore, get_store
from app.schemas import HealthResponse, IngestResponse, TriageRequest, TriageResponse

logger = logging.getLogger("opsmind")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().gemini_api_key:  # warm the index; failures must not block startup
        try:
            info = await run_in_threadpool(get_store().ingest)
            logger.info("Indexed %s chunks via %s", info.chunks, info.backend)
        except Exception:  # noqa: BLE001
            logger.exception("Startup ingest failed; call POST /ingest later")
    yield


app = FastAPI(
    title="OpsMind AI",
    version=__version__,
    description="Agentic RAG incident triage & runbook assistant.",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health(store: RunbookStore = Depends(get_store)) -> HealthResponse:
    es_up = await run_in_threadpool(store.es_available)
    return HealthResponse(
        status="ok",
        backend=store.backend,
        es_available=es_up,
        chunks_indexed=store.chunk_count,
        llm_configured=bool(get_settings().gemini_api_key),
    )


@app.post("/ingest", response_model=IngestResponse)
async def ingest(store: RunbookStore = Depends(get_store)) -> IngestResponse:
    """(Re)index all Markdown runbooks from RUNBOOKS_DIR."""
    try:
        return await run_in_threadpool(store.ingest)
    except ProviderError as e:
        raise HTTPException(503, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("ingest failed")
        raise HTTPException(502, "Embedding/indexing backend failed") from e


@app.post("/triage", response_model=TriageResponse)
async def triage(req: TriageRequest, graph: Any = Depends(get_graph)) -> TriageResponse:
    try:
        return await run_triage(graph, req.log)
    except ProviderError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("triage failed")
        raise HTTPException(502, "LLM or retrieval backend failed") from e
