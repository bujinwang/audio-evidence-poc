#!/usr/bin/env python3
"""Generate a realistic, noisy test set for upload.

The three files from `make_demo_data.py` are deliberately clean so the demo is
predictable. Real evidence is not clean: it is a 911 call through a phone codec,
a body camera in wind with the mic clipping, a warehouse with machinery running.
This script produces that, so transcription, denoising, and search can be tried
against material that behaves like the real thing.

Design:

* **Speech is synthesised line by line** with the same TTS machinery as the demo
  set (`make_demo_data`), so every segment's start/end is measured, not estimated.
  Noise is mixed underneath on the same timeline, so timestamps stay exact even
  at 4 dB SNR — which is what makes "jump to the timestamp" testable on hard audio.
* **Noise is generated in numpy**, not with ffmpeg filter graphs, so it is
  deterministic, inspectable, and reproducible from a seed. Profiles model the
  physical cause (traffic rumble, wind buffet, HVAC hum, tape hiss, radio murmur).
* **SNR is targeted, not guessed.** The speech RMS is measured after the channel
  processing, then the noise bed is scaled to hit the requested SNR. The achieved
  SNR is measured from the two components and written to the manifest, so these
  files double as a fixed-difficulty benchmark for ASR accuracy later.

Usage:
    python scripts/make_noisy_test_set.py
    python scripts/make_noisy_test_set.py --out ../data/test-audio
    python scripts/make_noisy_test_set.py --upload http://127.0.0.1:8000
    python scripts/make_noisy_test_set.py --only 911,warehouse

The files are generated, not uploaded: point the browser at
`data/test-audio/` and drag them in (select the matching `.transcript.json` in
the "Reference transcript" field so the pipeline has ground truth to work from).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import wave
import zlib
from pathlib import Path

import numpy as np

# Reuse the verified TTS layer from the demo generator. We deliberately import
# the private helpers rather than refactoring that file: its output is checked
# byte-for-byte by the shipped dataset, and a refactor there would risk the
# demo audio for no benefit here. If those helpers are renamed, this script
# fails loudly at import time, which is the behaviour we want.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_demo_data as demo  # noqa: E402

SR = demo.SAMPLE_RATE  # 16 kHz
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------- #
# noise building blocks — all deterministic given a seed
# --------------------------------------------------------------------------- #

def _spectral_noise(n: int, slope: float, rng: np.random.Generator) -> np.ndarray:
    """Noise with a 1/f^slope power spectrum: 1 = pink, 2 = brown, 0 = white."""
    if n <= 0:
        return np.zeros(0)
    white = rng.standard_normal(n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    freqs[0] = freqs[1] if len(freqs) > 1 else 20.0
    spectrum /= np.maximum(freqs, 20.0) ** (slope / 2.0)
    shaped = np.fft.irfft(spectrum, n)
    return _unit(shaped)


def _seed_for(text: str, salt: int = 0) -> int:
    """Stable seed. Python's built-in hash() is salted per process, which would
    make the noise different on every run — reproducibility is the whole point."""
    return zlib.crc32(f"{salt}:{text}".encode("utf-8"))


def _unit(signal: np.ndarray) -> np.ndarray:
    """Scale to unit RMS so layers can be mixed by weight."""
    rms = float(np.sqrt(np.mean(signal**2))) if signal.size else 0.0
    return signal / rms if rms > 1e-12 else signal


def _bandpass(signal: np.ndarray, low: float, high: float) -> np.ndarray:
    """Soft-edged FFT band-pass. Edges are logistic so there is no ringing."""
    if signal.size == 0:
        return signal
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(len(signal), 1.0 / SR)
    width = max(low, 1.0) * 0.25
    # Clamp before exp(): freqs can be thousands of Hz above the cut-off and
    # exp() overflows to inf, which turns the edge into 0/0.
    low_edge = 1.0 / (1.0 + np.exp(np.clip(-(freqs - low) / width, -60, 60)))
    high_edge = 1.0 / (1.0 + np.exp(np.clip((freqs - high) / (high * 0.25), -60, 60)))
    return np.fft.irfft(spectrum * low_edge * high_edge, len(signal))


def _hum(n: int, base_hz: float, rng: np.random.Generator, harmonics: int = 4) -> np.ndarray:
    """Mains hum plus harmonics — the signature of a bad ground or a big motor."""
    t = np.arange(n) / SR
    signal = np.zeros(n)
    for k in range(1, harmonics + 1):
        phase = rng.uniform(0, 2 * np.pi)
        signal += (1.0 / k) * np.sin(2 * np.pi * base_hz * k * t + phase)
    return _unit(signal)


def _modulate(signal: np.ndarray, rate_hz: float, depth: float,
              rng: np.random.Generator) -> np.ndarray:
    """Slow amplitude modulation, plus a random walk so it never sounds periodic."""
    t = np.arange(len(signal)) / SR
    phase = rng.uniform(0, 2 * np.pi)
    wave_env = 0.5 + 0.5 * np.sin(2 * np.pi * rate_hz * t + phase)
    drift = _bandpass(rng.standard_normal(len(signal)), 0.05, 0.8)
    drift = 0.5 + 0.5 * np.tanh(drift)
    envelope = (1.0 - depth) + depth * (0.6 * wave_env + 0.4 * drift)
    return signal * envelope


def _clanks(n: int, count: int, rng: np.random.Generator) -> np.ndarray:
    """Sparse metallic hits: short bursts of band-passed noise with a fast decay."""
    out = np.zeros(n)
    for _ in range(count):
        start = int(rng.uniform(0, max(n - SR // 2, 1)))
        length = int(rng.uniform(0.03, 0.14) * SR)
        burst = rng.standard_normal(length) * np.exp(-np.arange(length) / (length * 0.25))
        burst = _bandpass(burst, 700, 4200)
        out[start:start + length] += burst[: max(0, min(length, n - start))]
    return _unit(out) if out.any() else out


def _pass_bys(n: int, count: int, rng: np.random.Generator) -> np.ndarray:
    """Vehicles passing: low rumble swells with a slow attack and decay."""
    out = np.zeros(n)
    for _ in range(count):
        centre = int(rng.uniform(0, max(n - 1, 1)))
        width = int(rng.uniform(1.2, 2.8) * SR)
        start = max(0, centre - width // 2)
        end = min(n, start + width)
        if end <= start:
            continue
        envelope = np.hanning(end - start) ** 2
        out[start:end] += envelope
    rumble = _spectral_noise(n, 2.0, rng)
    rumble = _bandpass(rumble, 30, 420)
    return _unit(out * rumble + 0.15 * rumble)


def _murmur(n: int, rng: np.random.Generator) -> np.ndarray:
    """Muffled speech through a wall or a TV in the next room.

    Band-limited noise amplitude-modulated near the syllable rate (~4 Hz) reads
    as unintelligible conversation without needing real background speakers.
    """
    bed = _bandpass(_spectral_noise(n, 1.0, rng), 300, 2400)
    bed = _modulate(bed, 3.7, 0.85, rng)
    bed = _modulate(bed, 5.3, 0.45, rng)
    return _unit(bed)


# --------------------------------------------------------------------------- #
# noise profiles — what a scene sounds like
# --------------------------------------------------------------------------- #

def build_noise(profile: str, n: int, seed: int) -> np.ndarray:
    """Mix a named noise profile down to a single unit-RMS bed."""
    rng = np.random.default_rng(seed)
    layers: list[tuple[float, np.ndarray]] = []

    if profile == "phone_line":
        layers = [
            (1.0, _bandpass(_spectral_noise(n, 0.6, rng), 300, 3400)),
            (0.35, _hum(n, 60, rng)),
            (0.25, _murmur(n, rng)),
        ]
    elif profile == "bodycam_street":
        layers = [
            (1.0, _pass_bys(n, 3, rng)),
            (0.55, _modulate(_bandpass(_spectral_noise(n, 1.0, rng), 120, 1800), 0.35, 0.7, rng)),
            (0.30, _spectral_noise(n, 0.4, rng)),
        ]
    elif profile == "interview_room":
        layers = [
            (1.0, _hum(n, 60, rng, harmonics=5)),
            (0.70, _spectral_noise(n, 1.4, rng)),
            (0.25, _bandpass(_spectral_noise(n, 2.0, rng), 40, 300)),
        ]
    elif profile == "outdoor_canvass":
        layers = [
            (1.0, _modulate(_bandpass(_spectral_noise(n, 1.2, rng), 60, 2600), 0.22, 0.8, rng)),
            (0.45, _pass_bys(n, 1, rng)),
            (0.20, _bandpass(_spectral_noise(n, 0.5, rng), 900, 5000)),
        ]
    elif profile == "warehouse":
        layers = [
            (1.0, _hum(n, 120, rng, harmonics=4)),
            (0.75, _spectral_noise(n, 1.2, rng)),
            (0.55, _clanks(n, 7, rng)),
        ]
    elif profile == "voicemail_tape":
        layers = [
            (1.0, _bandpass(_spectral_noise(n, 0.3, rng), 300, 3400)),
            (0.55, _hum(n, 60, rng, harmonics=3)),
            (0.18, _clanks(n, 4, rng)),
        ]
    else:
        raise ValueError(f"unknown noise profile: {profile}")

    bed = np.zeros(n)
    for weight, layer in layers:
        if layer.size == n:
            bed += weight * _unit(layer)
    return _unit(bed) if bed.any() else bed


# --------------------------------------------------------------------------- #
# channel effects — what the recording device did to the speech
# --------------------------------------------------------------------------- #

def _reverb(signal: np.ndarray, decay_s: float, wet: float, seed: int) -> np.ndarray:
    """Small-room reverberation via convolution with a decaying noise impulse."""
    if wet <= 0:
        return signal
    rng = np.random.default_rng(seed)
    length = max(int(decay_s * SR), 8)
    impulse = rng.standard_normal(length) * np.exp(-np.arange(length) / (length * 0.28))
    impulse[0] = 1.0
    # FFT convolution: a direct np.convolve is O(n*m) and takes minutes on a
    # 47 s file at 16 kHz.
    size = 1 << (len(signal) + length - 1).bit_length()
    tail = np.fft.irfft(
        np.fft.rfft(signal, size) * np.fft.rfft(impulse, size), size
    )[: len(signal)]
    return (1.0 - wet) * signal + wet * _unit(tail) * _rms(signal)


def _soft_clip(signal: np.ndarray, drive: float) -> np.ndarray:
    """Mic overload: tanh saturation, which is what a cheap preamp does."""
    if drive <= 0:
        return signal
    return np.tanh(signal * drive) / np.tanh(drive)


def _rms(signal: np.ndarray) -> float:
    return float(np.sqrt(np.mean(signal**2))) if signal.size else 0.0


def _to_int16(signal: np.ndarray, peak: float = 0.89) -> np.ndarray:
    """Peak-normalise and convert. Peak (not RMS) so nothing clips."""
    maximum = float(np.max(np.abs(signal))) if signal.size else 0.0
    if maximum > 0:
        signal = signal * (peak / maximum)
    return np.clip(signal * 32767.0, -32768, 32767).astype("<i2")


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #

# Casting is deliberately different from the demo set: new voices so the corpus
# does not sound like the same four people, and so search results are visibly
# drawn from different recordings.
VOICES = {
    "SPEAKER_10": "en-US-BrianNeural",     # officer / detective
    "SPEAKER_11": "en-CA-ClaraNeural",     # caller
    "SPEAKER_12": "en-CA-LiamNeural",      # 911 dispatcher
    "SPEAKER_13": "en-US-GuyNeural",       # driver
    "SPEAKER_14": "en-US-EmmaNeural",      # witness
    "SPEAKER_15": "en-GB-SoniaNeural",     # elderly resident
    "SPEAKER_16": "en-GB-RyanNeural",      # warehouse worker
    "SPEAKER_17": "en-US-AvaNeural",       # anonymous tip
}

SCENARIOS = [
    {
        "slug": "911-shop-lot",
        "note": "Telephony channel: 300-3400 Hz band, line hum, muffled TV in the room.",
        "filename": "2026-09-02_911_call_shop-lot.wav",
        "source_type": "phone_call",
        "recorded_at": "2026-09-02T21:14:00-06:00",
        "noise": "phone_line",
        "snr_db": 12.0,
        "bandpass": (300.0, 3400.0),
        "reverb": (0.0, 0.0),
        "clip": 0.0,
        "lines": [
            ("SPEAKER_12", "Nine one one, what is your emergency?"),
            ("SPEAKER_11", "Someone just smashed the window of my store on King Street and ran toward the alley."),
            ("SPEAKER_12", "Are you injured? Is anyone still inside?"),
            ("SPEAKER_11", "No, I am alone, but he took two boxes from the display."),
            ("SPEAKER_12", "Can you describe him?"),
            ("SPEAKER_11", "Young, tall, grey hoodie, and he got into a blue pickup truck."),
            ("SPEAKER_12", "Did you get the plate?"),
            ("SPEAKER_11", "Only the first three letters, Bravo Tango Charlie."),
            ("SPEAKER_12", "Stay on the line, officers are on their way."),
        ],
    },
    {
        "slug": "bodycam-traffic-stop",
        "note": "Street: passing vehicles, wind buffet, mic saturation on peaks.",
        "filename": "2026-09-05_bodycam_traffic-stop.wav",
        "source_type": "bodycam",
        "recorded_at": "2026-09-05T23:48:00-06:00",
        "noise": "bodycam_street",
        "snr_db": 8.0,
        "bandpass": None,
        "reverb": (0.18, 0.10),
        "clip": 1.8,
        "lines": [
            ("SPEAKER_10", "Evening. Licence and registration, please."),
            ("SPEAKER_13", "Is there a problem, officer?"),
            ("SPEAKER_10", "Your tail light is out and you drifted over the centre line back there."),
            ("SPEAKER_13", "I did not realise. I am coming off a night shift."),
            ("SPEAKER_10", "Have you had anything to drink tonight?"),
            ("SPEAKER_13", "No, nothing at all."),
            ("SPEAKER_10", "Please step out of the vehicle and keep your hands where I can see them."),
        ],
    },
    {
        "slug": "interview-room-hvac",
        "note": "Baseline: HVAC hum and room reverb, otherwise clean.",
        "filename": "2026-09-11_interview_room_hvac.wav",
        "source_type": "interview",
        "recorded_at": "2026-09-11T14:05:00-06:00",
        "noise": "interview_room",
        "snr_db": 20.0,
        "bandpass": (80.0, 7000.0),
        "reverb": (0.30, 0.14),
        "clip": 0.0,
        "lines": [
            ("SPEAKER_10", "For the record, this interview is being recorded."),
            ("SPEAKER_14", "I understand."),
            ("SPEAKER_10", "You told the first officer you saw a blue pickup truck leave the lot."),
            ("SPEAKER_14", "Yes, it was parked by the loading door for maybe ten minutes."),
            ("SPEAKER_10", "Could you see who was driving?"),
            ("SPEAKER_14", "Only that he was wearing a light coloured jacket."),
        ],
    },
    {
        "slug": "canvass-door-knock",
        "note": "Outdoors: wind, distant traffic, a quiet elderly voice.",
        "filename": "2026-09-14_canvass_door-knock.wav",
        "source_type": "other",
        "recorded_at": "2026-09-14T17:32:00-06:00",
        "noise": "outdoor_canvass",
        "snr_db": 6.0,
        "bandpass": (90.0, 6500.0),
        "reverb": (0.12, 0.06),
        "clip": 0.0,
        "lines": [
            ("SPEAKER_10", "Good afternoon, I am canvassing the neighbourhood about Tuesday night."),
            ("SPEAKER_15", "I already spoke to a constable yesterday."),
            ("SPEAKER_10", "I know, I am following up on one detail. Did you hear a vehicle around eleven?"),
            ("SPEAKER_15", "A truck, yes. It was loud, and it sat idling for a while."),
            ("SPEAKER_10", "Did you see the colour?"),
            ("SPEAKER_15", "Dark, maybe blue. My eyes are not what they were."),
        ],
    },
    {
        "slug": "warehouse-loading-dock",
        "note": "Hardest: machinery hum, metal clanks, heavy reverb at 4 dB.",
        "filename": "2026-09-18_warehouse_loading-dock.wav",
        "source_type": "other",
        "recorded_at": "2026-09-18T00:20:00-06:00",
        "noise": "warehouse",
        "snr_db": 4.0,
        "bandpass": (60.0, 7000.0),
        "reverb": (0.55, 0.22),
        "clip": 0.6,
        "lines": [
            ("SPEAKER_16", "I was on the loading dock until about midnight."),
            ("SPEAKER_16", "A blue pickup came in through the north gate, no paperwork."),
            ("SPEAKER_16", "Two men unloaded four crates and left in a hurry."),
            ("SPEAKER_16", "I did not get a plate, it was too dark."),
        ],
    },
    {
        "slug": "voicemail-tip",
        "note": "Voicemail: tape hiss, line noise, one speaker, no turn changes.",
        "filename": "2026-09-21_voicemail_tip.wav",
        "source_type": "phone_call",
        "recorded_at": "2026-09-21T08:02:00-06:00",
        "noise": "voicemail_tape",
        "snr_db": 14.0,
        "bandpass": (300.0, 3400.0),
        "reverb": (0.0, 0.0),
        "clip": 0.0,
        "lines": [
            ("SPEAKER_17", "This is a message for the detective handling the King Street case."),
            ("SPEAKER_17", "I saw the truck again, parked behind the arena on Mill Road."),
            ("SPEAKER_17", "It has a silver toolbox in the back and a cracked rear window."),
            ("SPEAKER_17", "I would rather not leave my name."),
        ],
    },
]


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #

def synthesise_line(utterance, chain: tuple[str, ...], scratch: Path, index: int,
                    cache_dir: Path) -> tuple[bytes, str]:
    """Render one line through the shared chain.

    The retry-before-fallback logic used to live here, because this script hit
    the edge-tts ``NoAudioReceived`` throttling bug first. It now lives in
    ``demo._synthesise`` itself (``demo._render_with_retry``), so every caller
    gets it — including the demo generator, where the same bug silently spliced
    a compact ``say`` voice into a neural recording.

    Keeping a second copy here would nest the loops: the outer retry would
    re-run the inner one, turning 4 attempts into 16 and doubling the sleeps.
    This wrapper is therefore a thin alias, kept only so callers that import it
    by name continue to work.
    """
    return demo._synthesise(utterance, chain, scratch, index, cache_dir)


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build_scenario(spec: dict, out_dir: Path, engine: str, voice: str | None,
                   cache_dir: Path) -> dict:
    scratch = out_dir / f".scratch-{spec['slug']}"
    scratch.mkdir(parents=True, exist_ok=True)
    chain = demo.ENGINE_CHAINS[engine]

    lines: list[tuple[str, str]] = spec["lines"]
    clips: list[np.ndarray] = []
    segments: list[dict] = []
    cursor_frames = 0
    providers: dict[str, int] = {}

    # --- speech, line by line, exactly as the demo set does it ---------------
    for index, (speaker, text) in enumerate(lines):
        next_speaker = lines[index + 1][0] if index + 1 < len(lines) else speaker
        rate, pitch = demo._prosody(text)
        utterance = demo.Utterance(
            text=text, speaker=speaker, rate=rate, pitch=pitch,
            voice_override=voice or VOICES.get(speaker),
        )
        clip_bytes, provider = synthesise_line(utterance, chain, scratch, index, cache_dir)
        providers[provider] = providers.get(provider, 0) + 1

        clip = np.frombuffer(clip_bytes, dtype="<i2").astype(np.float64) / 32768.0
        clip_frames = len(clip)
        start = cursor_frames / SR
        end_frames = cursor_frames + clip_frames

        segments.append(
            {
                "start": round(start, 3),
                "end": round(end_frames / SR, 3),
                "text": text,
                "speaker": speaker,
                "words": demo._word_timings(text, start, clip_frames / SR),
            }
        )

        clips.append(clip)
        pause_frames = round(demo._pause_after(text, speaker, next_speaker) * SR)
        clips.append(np.zeros(pause_frames))
        cursor_frames = end_frames + pause_frames

    dry = np.concatenate(clips) if clips else np.zeros(1)
    total_frames = len(dry)

    # --- channel: what the device did to the speech -------------------------
    speech = dry.copy()
    band = spec.get("bandpass")
    if band:
        speech = _bandpass(speech, band[0], band[1])
    reverb_decay, reverb_wet = spec.get("reverb", (0.0, 0.0))
    speech = _reverb(speech, reverb_decay, reverb_wet, seed=_seed_for(spec["slug"], 1))
    if spec.get("clip"):
        speech = _soft_clip(speech, spec["clip"])

    # --- noise, scaled to the requested SNR ---------------------------------
    noise = build_noise(spec["noise"], total_frames, seed=_seed_for(spec["slug"], 2))
    speech_rms = _rms(speech)
    noise_rms = _rms(noise)
    target_snr = float(spec["snr_db"])
    if noise_rms > 1e-12:
        noise = noise * (speech_rms / (10.0 ** (target_snr / 20.0)) / noise_rms)

    mixed = speech + noise
    # This is the ratio actually baked into the file, not an independent
    # estimate: the bed was scaled from the measured speech RMS, so the two
    # must agree. Reported for traceability, not as a quality measurement.
    applied_snr = 20.0 * np.log10(speech_rms / max(_rms(noise), 1e-12))

    target = out_dir / spec["filename"]
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(_to_int16(mixed).tobytes())

    sidecar = target.with_suffix(".transcript.json")
    sidecar.write_text(
        json.dumps(
            {
                "language": "en",
                "source": "synthetic-noisy-test-set",
                "note": (
                    f"Reference transcript for a synthetic {spec['noise']} recording at "
                    f"{target_snr:.0f} dB SNR (applied, not estimated). Timestamps are exact: each line was "
                    f"synthesised separately and noise was mixed on the same timeline."
                ),
                "segments": segments,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    shutil.rmtree(scratch, ignore_errors=True)

    return {
        "slug": spec["slug"],
        "audio": str(target),
        "sidecar": str(sidecar),
        "filename": spec["filename"],
        "source_type": spec["source_type"],
        "recorded_at": spec["recorded_at"],
        "duration_s": round(total_frames / SR, 2),
        "segments": len(segments),
        "noise": spec["noise"],
        "note": spec.get("note", ""),
        "snr_db_target": target_snr,
        "snr_db_applied": round(applied_snr, 1),
        "bandpass_hz": list(band) if band else None,
        "providers": providers,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


def _readme(manifest: list[dict], out_dir: Path) -> str:
    """Self-describing folder: whoever opens it in Finder knows what to do."""
    rows = "\n".join(
        f"| `{e['filename']}` | {e['source_type']} | {e['duration_s']:.1f}s | "
        f"{e['segments']} | {e['noise']} | {e['snr_db_applied']:.0f} dB | {e['note']} |"
        for e in manifest
    )
    return f"""# Noisy test audio

Six synthetic recordings with realistic degradation, for trying the pipeline on
material that behaves like real evidence. Generated by
`backend/scripts/make_noisy_test_set.py` — do not hand-edit, regenerate instead.

| File | Source | Duration | Segments | Noise | SNR | What it tests |
|---|---|---|---|---|---|---|
{rows}

## Upload them

**Through the UI:** open the Library, drop a `.wav` in, and pick the matching
`.transcript.json` in the *Reference transcript* field — that gives the pipeline
ground truth without needing an ASR model installed.

**Or from the command line:**

```bash
cd backend && ./.venv/bin/python scripts/make_noisy_test_set.py --upload http://127.0.0.1:8000
```

## Things to try

- Search `blue pickup truck` — it appears in four of these recordings plus the
  clean demo set, so results should span recordings.
- Open the **hardest** file (warehouse, 4 dB) and check that timestamp playback
  still lands on the right sentence through the noise.
- Compare `interview_room_hvac` (20 dB) against `warehouse` (4 dB) to see where
  retrieval and transcription start to degrade.
- Toggle **Original vs denoised** once DeepFilterNet is installed — these files
  were built so denoising has something real to remove.

## Notes

- 16 kHz mono PCM, matching what the pipeline produces internally.
- Timestamps are exact: each line was synthesised separately and the noise bed
  was mixed on the same timeline, so segment boundaries are true even at 4 dB.
- SNR is *applied*, not estimated — the noise bed is scaled from the measured
  speech RMS, so the number is what the file contains by construction.
- Nothing here is real: no real people, places, or recordings.
"""


def upload(base_url: str, entry: dict) -> str | None:
    return demo.upload(base_url, entry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "test-audio"))
    parser.add_argument("--engine", default="auto", choices=sorted(demo.ENGINE_CHAINS))
    parser.add_argument("--voice", default=None, help="force one voice for every speaker")
    parser.add_argument("--only", default=None,
                        help="comma-separated slugs to build (default: all)")
    parser.add_argument("--upload", default=None, help="API base URL, e.g. http://127.0.0.1:8000")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = REPO_ROOT / "data" / "samples" / ".tts-cache"

    wanted = {s.strip() for s in args.only.split(",")} if args.only else None
    scenarios = [s for s in SCENARIOS if not wanted or s["slug"] in wanted]
    if not scenarios:
        print(f"no scenarios matched {args.only!r}", file=sys.stderr)
        return 2

    print(f"output: {out_dir}")
    print(f"engine: {args.engine}")
    print()
    print(f"{'file':<44} {'dur':>6} {'segs':>4} {'noise':<17} {'SNR':>6}")
    print("-" * 82)

    manifest = []
    for spec in scenarios:
        entry = build_scenario(spec, out_dir, args.engine, args.voice, cache_dir)
        manifest.append(entry)
        print(
            f"{entry['filename']:<44} {entry['duration_s']:>5.1f}s "
            f"{entry['segments']:>4} {entry['noise']:<17} "
            f"{entry['snr_db_applied']:>5.1f}dB"
        )

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "README.md").write_text(_readme(manifest, out_dir), encoding="utf-8")

    if args.upload:
        print(f"\nuploading to {args.upload}")
        for entry in manifest:
            recording_id = upload(args.upload, entry)
            print(f"- {entry['slug']}: {recording_id or 'FAILED'}")

    print("\nNext: open the app, go to Library, and drop a .wav in — select the matching")
    print(".transcript.json in the 'Reference transcript' field so the pipeline has ground truth.")
    print("Hardest first: warehouse (4 dB), canvass (6 dB), bodycam (8 dB).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
