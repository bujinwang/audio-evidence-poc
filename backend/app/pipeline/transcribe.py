"""Transcription providers behind one interface.

Resolution order for `TRANSCRIBER=auto`:

1. **WhisperX** — timestamps + optional word alignment + optional diarization.
2. **faster-whisper** — faster CPU inference, word timestamps, no diarization.
3. **sidecar** — a human/reference transcript shipped next to the audio
   (`<name>.transcript.json`). Used for the synthetic demo dataset and tests so
   the whole workflow is exercisable without downloading a model.
4. **none** — raises `DependencyUnavailable`; the job records the reason and the
   recording still exists, un-transcribed, in the library.

Every provider returns the same shape, so swapping one in is a config change.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DependencyUnavailable, ProcessingError
from ..storage import sidecar_candidates


@dataclass
class TranscriptionResult:
    segments: list[dict]
    language: str | None
    engine: str
    model_version: str
    settings: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return round(max((s["end"] for s in self.segments), default=0.0), 3)


class Transcriber:
    name = "none"
    model_version = "n/a"

    def available(self) -> tuple[bool, str]:
        return False, "not configured"

    def transcribe(self, audio: Path, *, hint: Path | None = None) -> TranscriptionResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# WhisperX
# --------------------------------------------------------------------------- #

class WhisperXTranscriber(Transcriber):
    name = "whisperx"

    def __init__(self, model_name: str, device: str, compute_type: str,
                 diarize: bool, hf_token: str, language: str = "en"):
        self.model_version = f"whisperx/{model_name}"
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.diarize = diarize
        self.hf_token = hf_token
        self.language = _asr_language(language)

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("whisperx") is None:
            return False, "whisperx not installed (`pip install whisperx`)"
        return True, f"whisperx ({self.model_name}, device={self.device})"

    def transcribe(self, audio: Path, *, hint: Path | None = None) -> TranscriptionResult:
        try:
            import whisperx
        except Exception as exc:  # pragma: no cover - env dependent
            raise DependencyUnavailable("whisperx", str(exc)) from exc

        device = self.device
        if device == "auto":
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        compute_type = "float16" if device == "cuda" else self.compute_type

        settings: dict = {"device": device, "compute_type": compute_type,
                          "batch_size": 8, "diarization": False,
                          "language": self.language or "auto"}
        warnings: list[str] = []

        try:
            model = whisperx.load_model(self.model_name, device, compute_type=compute_type)
            audio_data = whisperx.load_audio(str(audio))
            raw = model.transcribe(audio_data, batch_size=8, language=self.language)
            language = raw.get("language") or self.language
            try:
                align_model, metadata = whisperx.load_align_model(
                    language_code=language or "en", device=device
                )
                raw = whisperx.align(
                    raw["segments"], align_model, metadata, audio_data, device,
                    return_char_alignments=False,
                )
            except Exception as exc:  # alignment is best-effort
                warnings.append(f"word alignment unavailable: {exc}")

            if self.diarize and self.hf_token:
                try:
                    from whisperx.diarize import DiarizationPipeline

                    pipeline = DiarizationPipeline(token=self.hf_token, device=device)
                    raw = whisperx.assign_word_speakers(
                        pipeline(audio_data, min_speakers=None, max_speakers=None), raw
                    )
                    settings["diarization"] = True
                except Exception as exc:
                    warnings.append(f"diarization unavailable: {exc}")
            elif self.diarize and not self.hf_token:
                warnings.append("diarization requested but HF_TOKEN is not set")
        except Exception as exc:  # pragma: no cover - env dependent
            raise ProcessingError(f"WhisperX transcription failed: {exc}") from exc

        segments = [_normalise_segment(s, i) for i, s in enumerate(raw.get("segments", []))]
        settings["warnings"] = warnings
        return TranscriptionResult(
            segments=segments,
            language=language,
            engine="whisperx",
            model_version=f"whisperx/{self.model_name}",
            settings=settings,
        )


# --------------------------------------------------------------------------- #
# faster-whisper
# --------------------------------------------------------------------------- #

class FasterWhisperTranscriber(Transcriber):
    name = "faster-whisper"

    def __init__(self, model_name: str, device: str, compute_type: str,
                 language: str = "en"):
        self.model_version = f"faster-whisper/{model_name}"
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = _asr_language(language)

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("faster_whisper") is None:
            return False, "faster-whisper not installed (`pip install faster-whisper`)"
        return True, f"faster-whisper ({self.model_name})"

    def transcribe(self, audio: Path, *, hint: Path | None = None) -> TranscriptionResult:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover
            raise DependencyUnavailable("faster-whisper", str(exc)) from exc

        device = "cpu" if self.device == "auto" else self.device
        compute_type = "int8" if device == "cpu" else "float16"
        try:
            model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
            iterator, info = model.transcribe(
                str(audio), word_timestamps=True, vad_filter=True,
                language=self.language,
            )
            segments = []
            for i, seg in enumerate(iterator):
                segments.append(
                    {
                        "start": round(float(seg.start), 3),
                        "end": round(float(seg.end), 3),
                        "text": (seg.text or "").strip(),
                        "speaker": None,
                        "words": [
                            {"w": w.word.strip(), "start": round(float(w.start), 3),
                             "end": round(float(w.end), 3)}
                            for w in (seg.words or [])
                            if w.start is not None and w.end is not None
                        ],
                    }
                )
            language = getattr(info, "language", None)
        except Exception as exc:  # pragma: no cover - env dependent
            raise ProcessingError(f"faster-whisper transcription failed: {exc}") from exc

        return TranscriptionResult(
            segments=segments,
            language=language,
            engine="faster-whisper",
            model_version=f"faster-whisper/{self.model_name}",
            settings={"device": device, "compute_type": compute_type,
                      "word_timestamps": True, "vad_filter": True,
                      "language": self.language or "auto"},
        )


# --------------------------------------------------------------------------- #
# Reference / sidecar transcript
# --------------------------------------------------------------------------- #

class SidecarTranscriber(Transcriber):
    """Reads a prepared transcript next to the audio.

    Clearly labelled as a reference transcript everywhere it surfaces
    (`engine = "sidecar-reference"`) so it can never be mistaken for ASR output.
    """

    name = "sidecar-reference"
    model_version = "sidecar-reference/1"

    def __init__(self, path: Path):
        self.path = path

    def available(self) -> tuple[bool, str]:
        return True, f"reference transcript at {self.path.name}"

    def transcribe(self, audio: Path, *, hint: Path | None = None) -> TranscriptionResult:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProcessingError(f"sidecar transcript unreadable: {exc}") from exc

        raw_segments = payload.get("segments", payload if isinstance(payload, list) else [])
        segments = []
        for i, seg in enumerate(raw_segments):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            segments.append(
                {
                    "start": round(float(seg.get("start", 0.0)), 3),
                    "end": round(float(seg.get("end", 0.0)), 3),
                    "text": text,
                    "speaker": seg.get("speaker"),
                    "words": seg.get("words"),
                }
            )
        return TranscriptionResult(
            segments=segments,
            language=payload.get("language"),
            engine="sidecar-reference",
            model_version="sidecar-reference/1",
            settings=self._settings(payload),
        )

    def _settings(self, payload: dict) -> dict:
        """Provenance for the reference transcript, including voice synthesis.

        Synthetic demo recordings are voiced by a provider chain, so a build that
        fell back mid-recording mixes two voices in one file. The generator
        records that (``tts.providers`` / ``tts.mixed``); carrying it into the
        transcript settings makes the condition visible through the API instead
        of only in the sidecar file on disk.
        """
        settings: dict = {"source": self.path.name, "provider": "sidecar"}
        tts = payload.get("tts")
        if isinstance(tts, dict):
            settings["tts"] = {
                "providers": tts.get("providers") or {},
                "mixed": bool(tts.get("mixed")),
            }
        if payload.get("source"):
            settings["source_kind"] = payload["source"]
        return settings


class UnavailableTranscriber(Transcriber):
    name = "none"
    model_version = "n/a"

    def __init__(self, reason: str):
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def transcribe(self, audio: Path, *, hint: Path | None = None) -> TranscriptionResult:
        raise DependencyUnavailable("transcription", self.reason)


# --------------------------------------------------------------------------- #

def _asr_language(value: str | None) -> str | None:
    """Map a config value to a Whisper `language` argument.

    `auto`, `None`, or an empty string mean "let the model decide" (passed as
    `None`); anything else is a language code. Pinning a language is the safe
    default — see the note in `config.py` about the Welsh misdetection.
    """
    if value is None:
        return None
    cleaned = value.strip().lower()
    if cleaned in {"", "auto", "detect", "none"}:
        return None
    return cleaned


def _normalise_segment(seg: dict, index: int) -> dict:
    words = seg.get("words") or []
    return {
        "start": round(float(seg.get("start", 0.0)), 3),
        "end": round(float(seg.get("end", 0.0)), 3),
        "text": (seg.get("text") or "").strip(),
        "speaker": seg.get("speaker"),
        "words": [
            {
                "w": (w.get("word") or w.get("w") or "").strip(),
                "start": round(float(w.get("start", 0.0)), 3),
                "end": round(float(w.get("end", 0.0)), 3),
            }
            for w in words
            if w.get("start") is not None and w.get("end") is not None
        ],
    }


def find_sidecar(hint: Path | None) -> Path | None:
    if hint is None:
        return None
    for candidate in sidecar_candidates(hint):
        if candidate.exists():
            return candidate
    return None


def resolve_transcriber(
    audio: Path,
    *,
    preference: str = "auto",
    model_name: str = "base",
    device: str = "auto",
    compute_type: str = "int8",
    diarize: bool = False,
    hf_token: str = "",
    hint: Path | None = None,
    language: str = "en",
) -> Transcriber:
    """Pick a provider. `hint` is the original audio path used to locate a sidecar."""
    preference = (preference or "auto").lower()
    whisperx = WhisperXTranscriber(model_name, device, compute_type, diarize, hf_token,
                                   language=language)
    faster = FasterWhisperTranscriber(model_name, device, compute_type, language=language)
    sidecar_path = find_sidecar(hint or audio)

    if preference == "whisperx":
        return whisperx
    if preference in {"faster-whisper", "faster_whisper"}:
        return faster
    if preference in {"sidecar", "reference"}:
        return SidecarTranscriber(sidecar_path) if sidecar_path else UnavailableTranscriber(
            "TRANSCRIBER=sidecar but no `<name>.transcript.json` found next to the audio"
        )
    if preference in {"none", "off"}:
        return UnavailableTranscriber("transcription disabled by configuration")

    # auto
    if whisperx.available()[0]:
        return whisperx
    if faster.available()[0]:
        return faster
    if sidecar_path:
        return SidecarTranscriber(sidecar_path)
    return UnavailableTranscriber(
        "No ASR engine installed and no reference transcript found. "
        "Install WhisperX (`pip install -r requirements-asr.txt`) or supply "
        "`<recording>.transcript.json`."
    )
