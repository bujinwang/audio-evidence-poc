"""FFmpeg/ffprobe wrappers: probing and working-copy conversion.

Nothing here touches the original file. Outputs are always written to a new
path, and callers are responsible for recording the settings used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DependencyUnavailable, ProcessingError

TARGET_SAMPLE_RATE = 16_000
FFMPEG_TIMEOUT_S = 60 * 60


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    sample_rate: int | None
    channels: int | None
    codec: str | None
    container: str | None
    bit_rate: int | None
    raw: dict = field(default_factory=dict)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise DependencyUnavailable(
            "ffmpeg" if binary == "ffmpeg" else "ffprobe",
            f"`{binary}` not found on PATH. Install FFmpeg (see README).",
        )
    return path


def probe(path: Path) -> ProbeResult:
    binary = _require("ffprobe")
    cmd = [
        binary, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise ProcessingError(f"ffprobe timed out on {path.name}") from exc
    if proc.returncode != 0:
        raise ProcessingError(f"ffprobe failed: {proc.stderr.strip()[:400]}")

    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    duration = 0.0
    for candidate in (fmt.get("duration"), audio.get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue

    def _int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    return ProbeResult(
        duration_s=round(duration, 3),
        sample_rate=_int(audio.get("sample_rate")),
        channels=_int(audio.get("channels")),
        codec=audio.get("codec_name"),
        container=fmt.get("format_name"),
        bit_rate=_int(fmt.get("bit_rate")),
        raw={"format": fmt, "audio_stream": audio},
    )


def to_working_copy(src: Path, dst: Path, *, sample_rate: int = TARGET_SAMPLE_RATE) -> dict:
    """Convert to mono 16 kHz FLAC — the canonical input for ASR.

    Lossless so repeated transcription experiments see identical audio.
    """
    binary = _require("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "flac", str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    if proc.returncode != 0 or not dst.exists():
        raise ProcessingError(f"FFmpeg conversion failed: {proc.stderr.strip()[:400]}")
    return {
        "tool": "ffmpeg",
        "output": dst.name,
        "sample_rate": sample_rate,
        "channels": 1,
        "codec": "flac",
        "command": " ".join(cmd[4:]),
    }


def make_clip(src: Path, dst: Path, start_s: float, duration_s: float) -> dict:
    """Cut a passage for export. Reads the original, writes a new file."""
    binary = _require("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(start_s, 0):.3f}", "-t", f"{max(duration_s, 0.1):.3f}",
        "-i", str(src), "-vn", "-c:a", "flac", str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    if proc.returncode != 0 or not dst.exists():
        raise ProcessingError(f"FFmpeg clip failed: {proc.stderr.strip()[:400]}")
    return {"start_s": round(start_s, 3), "duration_s": round(duration_s, 3)}
