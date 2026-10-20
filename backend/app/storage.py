"""Filesystem layout and integrity helpers.

Rule: the original is written once, read-only, and never rewritten. Every
derivative path is derived from the recording id, never from the user-supplied
filename (which is stored as metadata only).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import get_settings

AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".oga", ".opus",
    ".aiff", ".aif", ".aifc", ".wma", ".amr", ".mp4", ".webm",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str) -> str:
    base = Path(name or "recording").name
    cleaned = _UNSAFE.sub("_", base).strip(" .") or "recording"
    return cleaned[:180]


def is_supported_audio(filename: str) -> bool:
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def original_path(recording_id: str, filename: str) -> Path:
    settings = get_settings()
    directory = settings.originals_dir / recording_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / safe_filename(filename)


def working_path(recording_id: str) -> Path:
    settings = get_settings()
    settings.working_dir.mkdir(parents=True, exist_ok=True)
    return settings.working_dir / f"{recording_id}.flac"


def denoised_path(recording_id: str) -> Path:
    settings = get_settings()
    settings.denoised_dir.mkdir(parents=True, exist_ok=True)
    return settings.denoised_dir / f"{recording_id}.denoised.flac"


def sidecar_candidates(audio: Path) -> list[Path]:
    """Reference transcript locations checked by the sidecar provider."""
    stem = audio.with_suffix("")
    return [
        audio.with_suffix(".transcript.json"),
        stem.with_suffix(".transcript.json"),
        audio.parent / f"{audio.name}.transcript.json",
        audio.parent / "sidecar" / f"{audio.stem}.transcript.json",
    ]


def sidecar_target(original: Path) -> Path:
    """Where an uploaded reference transcript is stored (next to the original)."""
    return original.with_suffix(".transcript.json")


def lock_original_readonly(path: Path) -> None:
    """Best-effort write protection for the source recording."""
    try:
        path.chmod(0o444)
    except OSError:  # pragma: no cover - e.g. exotic filesystems
        pass
