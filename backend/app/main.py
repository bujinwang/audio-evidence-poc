"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import db
from .config import REPO_ROOT, get_settings
from .db import get_session
from .pipeline import audio as audio_tools
from .pipeline.denoise import get_denoiser
from .pipeline.embed import get_embedder
from .pipeline.transcribe import FasterWhisperTranscriber, WhisperXTranscriber
from .pipeline.vectorstore import get_vector_store
from .routers import audit as audit_router
from .routers import recordings as recordings_router
from .routers import review as review_router
from .routers import search as search_router
from .schemas import ActorOut, CapabilityOut, HealthOut
from .security import Actor, get_actor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("audio-evidence")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    settings = get_settings()
    logger.info("data dir: %s", settings.data_dir)
    logger.info("database: %s", settings.database_url.split("@")[-1])
    logger.info("inline processing: %s", settings.process_inline)
    yield


app = FastAPI(
    title="AI Audio Evidence Search & Review",
    version="0.1.0",
    description=(
        "PoC for Technology North: ingest audio evidence, transcribe it, search it in "
        "natural language, play back the original at the matched timestamp, and review "
        "the transcript with a full audit trail."
    ),
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_origins) or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

app.include_router(recordings_router.router)
app.include_router(search_router.router)
app.include_router(review_router.router)
app.include_router(audit_router.router)


@app.get("/api/me", response_model=ActorOut, tags=["system"])
def me(actor: Actor = Depends(get_actor)) -> ActorOut:
    return ActorOut(actor=actor.name, role=actor.role, permissions=sorted(actor.permissions))


@app.get("/api/health", response_model=HealthOut, tags=["system"])
def health(session: Session = Depends(get_session)) -> HealthOut:
    settings = get_settings()
    capabilities: list[CapabilityOut] = []

    database_ok = db.healthcheck()
    capabilities.append(
        CapabilityOut(
            name="database",
            available=database_ok,
            detail="PostgreSQL" if not settings.is_sqlite else "SQLite (demo fallback)",
            kind="core",
        )
    )

    ffmpeg_ok = audio_tools.ffmpeg_available()
    capabilities.append(
        CapabilityOut(
            name="ffmpeg",
            available=ffmpeg_ok,
            detail="working-copy conversion and clip export" if ffmpeg_ok
            else "FFmpeg not on PATH — conversion and clip export disabled",
            kind="core",
        )
    )

    whisperx = WhisperXTranscriber(
        settings.whisper_model, settings.whisper_device,
        settings.whisper_compute_type, settings.diarization, settings.hf_token,
        language=settings.whisper_language,
    )
    faster = FasterWhisperTranscriber(
        settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
        language=settings.whisper_language,
    )
    asr_ok, asr_detail = whisperx.available()
    if not asr_ok:
        asr_ok, asr_detail = faster.available()
    capabilities.append(
        CapabilityOut(
            name="transcription",
            available=asr_ok,
            detail=(f"{asr_detail}, language={settings.whisper_language}"
                    if asr_ok else (
                        f"{asr_detail}. Reference transcripts (sidecar JSON) are "
                        "used when present."
                    )),
            kind="optional",
        )
    )

    denoiser = get_denoiser(settings.denoiser)
    denoise_ok, denoise_detail = denoiser.available()
    capabilities.append(
        CapabilityOut(name="denoising", available=denoise_ok, detail=denoise_detail, kind="optional")
    )

    embedder = get_embedder(settings.embedder, settings.embedding_model)
    capabilities.append(
        CapabilityOut(
            name="embeddings",
            available=True,
            detail=f"{embedder.name} (dim {embedder.dim}"
                   f"{'' if embedder.is_semantic else ', lexical fallback'})",
            kind="core",
        )
    )

    store = get_vector_store(
        session,
        preference=settings.vector_store,
        url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        dim=embedder.dim,
    )
    store_ok, store_detail = store.health()
    capabilities.append(
        CapabilityOut(name="vector_store", available=store_ok, detail=store_detail, kind="core")
    )

    hard_fail = not database_ok
    return HealthOut(
        status="degraded" if hard_fail else "ok",
        database="up" if database_ok else "down",
        vector_store=store.name,
        embedder=embedder.name,
        transcriber="whisperx" if whisperx.available()[0]
        else ("faster-whisper" if faster.available()[0] else "none"),
        denoiser="deepfilternet" if denoise_ok else "none",
        capabilities=capabilities,
    )


@app.get("/api/config", tags=["system"])
def config() -> dict:
    settings = get_settings()
    return {
        "max_upload_mb": settings.max_upload_mb,
        "source_types": ["interview", "bodycam", "cassette", "phone_call", "other"],
        "review_statuses": ["ai_generated", "in_review", "corrected", "verified"],
        "search_modes": ["keyword", "semantic", "hybrid"],
        "inline_processing": settings.process_inline,
        "semantic_embeddings": get_embedder(
            settings.embedder, settings.embedding_model
        ).is_semantic,
        "disclaimer": (
            "AI output is not evidence of truth. Transcripts are machine-generated until a "
            "reviewer verifies them; verification is recorded per segment."
        ),
    }


# Serve the built SPA when it exists (docker image / `npm run build`).
_dist = REPO_ROOT / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="ui")
else:
    logger.info("frontend build not found at %s — API only", _dist)
