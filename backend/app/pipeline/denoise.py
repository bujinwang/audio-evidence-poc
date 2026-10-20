"""Denoising provider.

DeepFilterNet is the PRD's choice. It is treated as an optional enhancement:
if it is not installed the job records `skipped` with the reason and the
pipeline continues on the original audio. The original always remains the
reference recording — denoising is never assumed to be an improvement.

Two integration paths exist, and they behave differently:

* **Python package** (`pip install deepfilternet`) — `df.enhance`, reads/writes
  audio via its own helpers. Pulls torch and pins numpy<2, so it is not the
  default route here.
* **`deepFilter` CLI** (Rust binary, 27 MB) — no Python dependencies at all.
  Two quirks handled below: it only reads **WAV** (the Hound crate), and
  without `-D` the output lags the input by a few milliseconds, which would
  misalign the original and denoised transcripts that the comparison screen
  puts side by side.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..errors import ProcessingError
from .audio import TARGET_SAMPLE_RATE


@dataclass(frozen=True)
class DenoiseResult:
    path: Path
    engine: str
    settings: dict


def _transcode(src: Path, dst: Path, *, codec: str) -> None:
    """Convert to 16 kHz mono. deepFilter needs WAV in; we keep FLAC on disk."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", codec, str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    if proc.returncode != 0 or not dst.exists():
        raise ProcessingError(f"ffmpeg transcode failed: {proc.stderr.strip()[:300]}")


def _mean_volume_db(path: Path) -> float | None:
    """Integrated RMS level in dBFS, via ffmpeg's `volumedetect`."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    for line in reversed((proc.stderr or "").splitlines()):
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except ValueError:
                return None
    return None


def _match_level(reference: Path, target: Path) -> float:
    """Bring `target` to the RMS level of `reference`, in place.

    DeepFilterNet does not preserve level: measured on AMI distant-microphone
    audio it cut RMS to 27% of the input and dropped the active-speech fraction
    from 47% to 16%. Whisper reacts badly to that — the same clip scored 95.2%
    WER denoised versus 72.8% once the level was restored, with no change to the
    filtering itself. Left uncorrected, every denoised comparison in this app
    measures the level drop rather than the denoising.

    Matching RMS is the minimum needed for a fair comparison. It is not
    loudness normalisation (no EBU R128), and it deliberately does nothing about
    the *pauses*, which is where the real suppression damage lives.
    """
    ref_db = _mean_volume_db(reference)
    tgt_db = _mean_volume_db(target)
    if ref_db is None or tgt_db is None:
        return 0.0
    gain_db = round(ref_db - tgt_db, 2)
    if abs(gain_db) < 0.5:
        return 0.0
    # Cap the boost so a near-silent filter output cannot be amplified into hiss.
    # Measured attenuation on a pure-tone test signal was 27.5 dB, so the cap has
    # to sit above that or the correction silently stops short of matching.
    gain_db = max(-30.0, min(gain_db, 36.0))
    staged = target.with_suffix(".level" + target.suffix)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(target),
         "-af", f"volume={gain_db}dB", "-c:a", "flac", str(staged)],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    if proc.returncode != 0 or not staged.exists():
        raise ProcessingError(f"level matching failed: {proc.stderr.strip()[:300]}")
    staged.replace(target)
    return gain_db


@lru_cache(maxsize=1)
def _deepfilter_version() -> str:
    """Recorded in processing metadata so a transcript can be traced to a build."""
    binary = shutil.which("deepFilter")
    if not binary:
        return "unknown"
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        first = (proc.stdout or proc.stderr).strip().splitlines()
        return first[0][:64] if first else "unknown"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


class Denoiser:
    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, "not configured"

    def denoise(self, src: Path, dst: Path) -> DenoiseResult:  # pragma: no cover - interface
        raise NotImplementedError


class DeepFilterNetDenoiser(Denoiser):
    name = "deepfilternet"

    def available(self) -> tuple[bool, str]:
        if importlib.util.find_spec("df") is not None:
            return True, "python package `df` (DeepFilterNet)"
        if shutil.which("deepFilter"):
            return True, f"`deepFilter` CLI on PATH ({_deepfilter_version()})"
        return False, (
            "DeepFilterNet not installed. `pip install deepfilternet` "
            "or install the `deepFilter` binary."
        )

    def denoise(self, src: Path, dst: Path) -> DenoiseResult:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if importlib.util.find_spec("df") is not None:
            return self._denoise_python(src, dst)
        if shutil.which("deepFilter"):
            return self._denoise_cli(src, dst)
        ok, reason = self.available()
        raise ProcessingError(reason)

    def _denoise_python(self, src: Path, dst: Path) -> DenoiseResult:
        try:
            import numpy as np  # noqa: F401
            from df.enhance import enhance, init_df, load_audio, save_audio
        except Exception as exc:  # pragma: no cover - env dependent
            raise ProcessingError(f"DeepFilterNet import failed: {exc}") from exc
        try:
            model, df_state, _ = init_df()
            audio, _ = load_audio(str(src), sr=df_state.sr())
            enhanced = enhance(model, df_state, audio)
            save_audio(str(dst), enhanced, df_state.sr())
        except Exception as exc:  # pragma: no cover - env dependent
            raise ProcessingError(f"DeepFilterNet failed: {exc}") from exc
        # Same level correction as the CLI path: the python API also returns a
        # heavily attenuated signal, and WER tracks the level more than the mask.
        gain_db = _match_level(src, dst)
        return DenoiseResult(
            path=dst,
            engine="deepfilternet",
            settings={"api": "df.enhance", "output_sample_rate": df_state.sr(),
                      "level_matched_db": gain_db},
        )

    def _denoise_cli(self, src: Path, dst: Path) -> DenoiseResult:  # pragma: no cover - env dependent
        binary = shutil.which("deepFilter")
        if not binary:
            raise ProcessingError("`deepFilter` not found on PATH")

        with tempfile.TemporaryDirectory(prefix="deepfilter-") as scratch_name:
            scratch = Path(scratch_name)
            # The 0.5.x CLI decodes with Hound, which is WAV-only — feeding it
            # our FLAC working copy fails with "no RIFF tag found".
            wav_in = scratch / "input.wav"
            _transcode(src, wav_in, codec="pcm_s16le")

            outdir = scratch / "out"
            outdir.mkdir()
            # -D compensates the model's STFT lookahead. Without it the output
            # lags the input (measured ~6 ms here), and the original/denoised
            # transcripts would no longer line up on the same timeline.
            proc = subprocess.run(
                [binary, "-D", str(wav_in), "-o", str(outdir)],
                capture_output=True, text=True, timeout=3600, check=False,
            )
            produced = sorted(outdir.glob("*.wav"))
            if proc.returncode != 0 or not produced:
                detail = (proc.stderr or proc.stdout or "").strip()[:300]
                raise ProcessingError(f"deepFilter failed: {detail or 'no output produced'}")

            # 0.5.6 keeps the input filename; older builds append _DeepFilterNet3.
            # Taking whatever single file appeared handles both.
            _transcode(produced[0], dst, codec="flac")

            # Restore the input's RMS level — DeepFilterNet attenuates heavily
            # (measured 27% of input RMS on AMI room-mic audio) and the level
            # loss alone costs more WER than the filtering recovers.
            gain_db = _match_level(src, dst)

        return DenoiseResult(
            path=dst,
            engine="deepfilternet-cli",
            settings={
                "cli": "deepFilter",
                "version": _deepfilter_version(),
                "flags": "-D (delay compensated)",
                "output_sample_rate": TARGET_SAMPLE_RATE,
                "level_matched_db": gain_db,
            },
        )


class NullDenoiser(Denoiser):
    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, "denoising disabled by configuration (DENOISER=none)"

    def denoise(self, src: Path, dst: Path) -> DenoiseResult:  # pragma: no cover
        raise ProcessingError("denoising disabled")


def get_denoiser(preference: str = "auto") -> Denoiser:
    preference = (preference or "auto").lower()
    if preference in {"none", "off", "disabled"}:
        return NullDenoiser()
    candidates: list[Denoiser] = [DeepFilterNetDenoiser()]
    if preference in {"auto", "deepfilternet", "deepfilter"}:
        return candidates[0]
    return NullDenoiser()
