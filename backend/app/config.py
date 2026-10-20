"""Application configuration.

Everything is environment-driven so the same image runs in a laptop demo and in
a locked-down deployment. Component selection (transcriber, denoiser, embedder,
vector store) is deliberately *not* hard-coded: each has an `auto` mode that
probes what is actually installed and degrades with a recorded reason instead of
crashing the pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Settings:
    env: str
    data_dir: Path
    database_url: str
    max_upload_mb: int

    # component selection
    transcriber: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str
    diarization: bool
    hf_token: str
    denoiser: str
    embedder: str
    embedding_model: str
    vector_store: str
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str

    # processing
    process_inline: bool
    worker_poll_seconds: float
    search_default_limit: int
    semantic_min_score: float

    # demo / dev conveniences
    dev_default_actor: str
    dev_default_role: str
    allow_anonymous: bool
    cors_origins: tuple[str, ...]

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def working_dir(self) -> Path:
        return self.data_dir / "working"

    @property
    def denoised_dir(self) -> Path:
        return self.data_dir / "denoised"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def _build() -> Settings:
    data_dir = _path("DATA_DIR", REPO_ROOT / "data")
    database_url = os.getenv("DATABASE_URL") or f"sqlite:///{data_dir / 'app.db'}"
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:8000")
    return Settings(
        env=os.getenv("APP_ENV", "dev"),
        data_dir=data_dir,
        database_url=database_url,
        max_upload_mb=_int("MAX_UPLOAD_MB", 512),
        transcriber=os.getenv("TRANSCRIBER", "auto").lower(),
        # `small` measured 3.9% WER on the noisy test set vs 4.7% for `base`,
        # at ~2.5x the compute — still 6x faster than realtime on CPU. Accuracy
        # matters more than throughput for evidence, so it is the default.
        # Use `base` for quick smoke tests.
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        whisper_device=os.getenv("WHISPER_DEVICE", "auto").lower(),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        # Language autodetection fails on short, quiet, or accented windows and
        # when it fails it fails *silently and catastrophically*: on AMI
        # distant-microphone audio it picked Welsh at p=0.73 and emitted
        # gibberish, scoring 77.0% WER where forcing English gave 40.4%.
        # An evidence transcript must not be in the wrong language, so `en` is
        # the default and `auto` is opt-in.
        whisper_language=os.getenv("WHISPER_LANGUAGE", "en").lower(),
        diarization=_bool("DIARIZATION", False),
        hf_token=os.getenv("HF_TOKEN", ""),
        denoiser=os.getenv("DENOISER", "auto").lower(),
        embedder=os.getenv("EMBEDDER", "auto").lower(),
        embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        vector_store=os.getenv("VECTOR_STORE", "auto").lower(),
        qdrant_url=os.getenv("QDRANT_URL", ""),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "evidence_segments"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
        process_inline=_bool("PROCESS_INLINE", True),
        worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "1.0")),
        search_default_limit=_int("SEARCH_DEFAULT_LIMIT", 20),
        semantic_min_score=float(os.getenv("SEMANTIC_MIN_SCORE", "0.05")),
        dev_default_actor=os.getenv("DEV_DEFAULT_ACTOR", "demo.investigator"),
        dev_default_role=os.getenv("DEV_DEFAULT_ROLE", "admin").lower(),
        allow_anonymous=_bool("ALLOW_ANONYMOUS", True),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = _build()
    for directory in (
        settings.data_dir,
        settings.originals_dir,
        settings.working_dir,
        settings.denoised_dir,
        settings.data_dir / "exports",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def reset_settings_cache() -> None:
    """Test helper: re-read the environment."""
    get_settings.cache_clear()
