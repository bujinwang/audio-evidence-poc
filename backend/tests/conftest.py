"""Test harness.

The environment is configured *before* any application module is imported, so
`db.engine` and the cached settings point at a throwaway SQLite database and a
throwaway data directory.
"""

from __future__ import annotations

import json
import math
import os
import struct
import tempfile
import wave
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="audio-evidence-tests-"))
_DATA_DIR = _TMP_ROOT / "data"
_DB_PATH = _TMP_ROOT / "test.db"

os.environ.update(
    {
        "APP_ENV": "test",
        "DATA_DIR": str(_DATA_DIR),
        "DATABASE_URL": f"sqlite:///{_DB_PATH}",
        "PROCESS_INLINE": "1",
        "TRANSCRIBER": "sidecar",
        "DENOISER": "none",
        "EMBEDDER": "hashing",
        "VECTOR_STORE": "local",
        "ALLOW_ANONYMOUS": "1",
        "DEV_DEFAULT_ACTOR": "test.admin",
        "DEV_DEFAULT_ROLE": "admin",
        "MAX_UPLOAD_MB": "8",
    }
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db  # noqa: E402
from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.main import app  # noqa: E402

SAMPLE_RATE = 16_000
ADMIN_HEADERS = {"X-Actor": "test.admin", "X-Role": "admin"}
REVIEWER_HEADERS = {"X-Actor": "test.reviewer", "X-Role": "reviewer"}
VIEWER_HEADERS = {"X-Actor": "test.viewer", "X-Role": "viewer"}


# --------------------------------------------------------------------------- #
# database isolation
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def clean_database():
    db.init_db()
    with db.engine.begin() as conn:
        for table in (
            "cue_candidates",
            "revisions",
            "audit_events",
            "segment_embeddings",
            "segments",
            "transcripts",
            "jobs",
            "recordings",
            "cases",
        ):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM segments_fts"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def set_env(monkeypatch):
    """Change configuration for one test (pipeline component selection)."""

    def _apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        reset_settings_cache()
        get_settings()

    yield _apply
    # Undo the env changes *before* clearing the cache, otherwise the stale
    # settings object stays cached for the next test.
    monkeypatch.undo()
    reset_settings_cache()
    get_settings()


# --------------------------------------------------------------------------- #
# sample media
# --------------------------------------------------------------------------- #

def write_sample(
    directory: Path,
    stem: str,
    lines: list[tuple[str, str]],
    *,
    sample_rate: int = SAMPLE_RATE,
) -> tuple[Path, Path]:
    """Write a WAV whose duration matches the reference transcript exactly."""
    directory.mkdir(parents=True, exist_ok=True)
    pcm = bytearray()
    segments: list[dict] = []
    cursor = 0.0

    for speaker, phrase in lines:
        duration = 0.26 * len(phrase.split()) + 0.6
        total = int(duration * sample_rate)
        fade = 400
        for index in range(total):
            envelope = min(1.0, index / fade, (total - index) / fade)
            value = int(6000 * envelope * math.sin(2 * math.pi * 190 * index / sample_rate))
            pcm += struct.pack("<h", value)
        segments.append(
            {
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "text": phrase,
                "speaker": speaker,
                "words": [
                    {"w": word, "start": round(cursor, 3), "end": round(cursor + duration, 3)}
                    for word in phrase.split()
                ],
            }
        )
        cursor += duration

    audio_path = directory / f"{stem}.wav"
    with wave.open(str(audio_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(pcm))

    sidecar_path = directory / f"{stem}.transcript.json"
    sidecar_path.write_text(
        json.dumps({"language": "en", "segments": segments}, indent=2), encoding="utf-8"
    )
    return audio_path, sidecar_path


WITNESS_LINES = [
    ("SPEAKER_00", "This is Detective Morrow speaking with witness Carla Nadeau."),
    ("SPEAKER_00", "Can you tell me what you saw near the community centre?"),
    ("SPEAKER_01", "I saw a blue pickup truck pull up beside the curb."),
    ("SPEAKER_01", "It had a cracked rear window and a silver toolbox."),
    ("SPEAKER_00", "Did you get a clear look at the driver?"),
]

BODYCAM_LINES = [
    ("SPEAKER_00", "Four twelve to dispatch, arriving at the community centre lot."),
    ("SPEAKER_00", "I have a blue pickup truck parked on the north side."),
    ("SPEAKER_02", "Copy four twelve, stand by for the registration result."),
]

CASSETTE_LINES = [
    ("SPEAKER_00", "This recording is a copy of a statement given in nineteen ninety eight."),
    ("SPEAKER_01", "The witness described a dark pickup truck with a loud exhaust."),
]


@pytest.fixture
def witness_sample(tmp_path: Path) -> tuple[Path, Path]:
    return write_sample(tmp_path / "media", "witness", WITNESS_LINES)


@pytest.fixture
def bodycam_sample(tmp_path: Path) -> tuple[Path, Path]:
    return write_sample(tmp_path / "media", "bodycam", BODYCAM_LINES)


@pytest.fixture
def cassette_sample(tmp_path: Path) -> tuple[Path, Path]:
    return write_sample(tmp_path / "media", "cassette", CASSETTE_LINES)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def upload(
    client: TestClient,
    sample: tuple[Path, Path],
    *,
    source_type: str = "interview",
    recorded_at: str = "2026-08-14T22:42:00-06:00",
    headers: dict | None = None,
    with_sidecar: bool = True,
):
    audio_path, sidecar_path = sample
    files = {"file": (audio_path.name, audio_path.read_bytes(), "audio/wav")}
    if with_sidecar:
        files["transcript_file"] = (
            sidecar_path.name,
            sidecar_path.read_bytes(),
            "application/json",
        )
    return client.post(
        "/api/recordings",
        files=files,
        data={"source_type": source_type, "recorded_at": recorded_at},
        headers=headers or ADMIN_HEADERS,
    )


def upload_ok(client: TestClient, sample, **kwargs) -> dict:
    response = upload(client, sample, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()
