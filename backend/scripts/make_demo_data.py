#!/usr/bin/env python3
"""Generate a synthetic, non-sensitive demo dataset.

Three recordings modelled on the PRD's demo scenario ("blue pickup truck"):

  1. witness interview  — clean speech
  2. body-camera audio  — clean speech, shorter segments
  3. "cassette" transfer — speech with pink-noise hiss, so denoising has
     something to actually do

Each recording gets a `<name>.transcript.json` reference transcript with exact
per-segment and per-word timestamps, produced by synthesising one line at a
time and concatenating with known gaps. That means the timestamps are true, not
estimated — the sidecar provider and the playback jump are genuinely verifiable.

Speech comes from a provider chain, so the demo does not sound like a single
synthetic narrator reading a phone book:

  ``edge``  Microsoft neural voices through ``edge-tts`` (best quality, needs
            network on the first run).
  ``say``   macOS ``say``, preferring an installed Premium/Enhanced voice over
            the default compact one (which is what made earlier builds robotic).
  ``tone``  tone bursts of the right duration — the last-resort fallback, so the
            pipeline and the UI still work offline with correct timings.

Every provider fails soft: an exception, timeout or network error moves on to
the next provider and prints one short line to stderr. TTS problems must never
break the dataset build.

Stepping down the chain is the last resort, not the first response, because a
step down is audible: it drops a different voice into the middle of a recording.
``edge`` throttles on bursts and reports ``NoAudioReceived``, which looks like a
hard failure but is not — the identical line succeeds seconds later. So ``edge``
is retried with exponential backoff before the chain is allowed to move on, and
an exhausted retry prints a ``mixed voice`` warning on stderr. Only ``edge`` is
retried: ``say`` fails deterministically (no binary, no voice) and ``tone``
cannot fail at all.

Transient means *retry*, and a cache hit is not a failure at all: cached lines
are returned before the retry logic runs, so a warm rebuild stays instant.

Radio traffic (dispatch, ``SPEAKER_02``) is band-limited through an ffmpeg
filter before its duration is measured, so it sounds like it came over a radio
and not out of the narrator's mouth.

Synthesised lines are cached under ``<out>/.tts-cache`` keyed by a hash of
(provider, text, voice, rate, pitch), so re-runs are instant and work offline.
A rebuild from that cache is byte-identical. A *cold* rebuild is not: it
re-renders through the neural service, which is not bit-reproducible, so sample
values vary very slightly between cold runs (measured correlation 0.998-0.999).
Frame counts — and therefore every timestamp in the sidecars — stay identical
either way. The cache is derived data and is always safe to delete.

Usage:
    python scripts/make_demo_data.py
    python scripts/make_demo_data.py --out ../data/samples
    python scripts/make_demo_data.py --engine say
    python scripts/make_demo_data.py --engine edge --voice en-CA-ClaraNeural
    python scripts/make_demo_data.py --list-voices
    python scripts/make_demo_data.py --upload http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SAMPLE_RATE = 16_000
GAP_SECONDS = 0.45
PAUSE_SPEAKER_CHANGE = 0.75
PAUSE_AFTER_QUESTION = 0.6
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Neural synthesis is a network round-trip per line; a hung socket would stall
# the whole build, so every edge-tts call is bounded.
EDGE_TIMEOUT_S = 30.0

# edge-tts throttles when requests arrive in a burst and answers with
# `NoAudioReceived`. That looks like a permanent failure but is not: the same
# line succeeds seconds later. Retrying is not politeness — it is the difference
# between a neural recording and one with a compact synthetic voice spliced into
# the middle of it, which is precisely the defect the move to edge-tts fixed.
#
# These values mirror scripts/make_noisy_test_set.py, where the same bug was
# already hit and fixed; the two generators must agree or one of them regresses
# silently.
EDGE_ATTEMPTS = 4
# Pacing the request start prevents most throttling; the backoff recovers from
# the rest. 0.4 s per *uncached* line costs a few seconds on a cold build and
# nothing at all on a warm one.
EDGE_REQUEST_PAUSE_S = 0.4
EDGE_BACKOFF_S = 2.0

# Providers whose failures are worth another attempt. `say` fails
# deterministically (no binary, no voice) and `tone` cannot fail at all, so
# retrying either only burns wall-clock — and `--engine say`/`--engine tone`
# must stay instant.
RETRYABLE_PROVIDERS = frozenset({"edge"})

# Failures that cannot possibly succeed on a second attempt because the thing
# is simply absent: the edge-tts package is not installed, or ffmpeg is missing.
# Retrying these would add ~14 s of sleeps per line to an offline build that is
# going to fall back anyway.
PERMANENT_ERRORS = (ImportError, FileNotFoundError)

# Speaker -> edge-tts voice. Canadian English on purpose: the deployment context
# is Alberta, and en-CA-* only ships Clara (female) and Liam (male) — there is no
# Canadian male "authority" voice for radio traffic, so dispatch borrows a US one.
VOICE_MAP = {
    "SPEAKER_00": "en-CA-LiamNeural",   # Detective Morrow, interviewer
    "SPEAKER_01": "en-CA-ClaraNeural",  # Carla Nadeau, witness
    "SPEAKER_02": "en-US-AndrewNeural",  # dispatch / radio
}
FALLBACK_EDGE_VOICE = "en-CA-LiamNeural"

# Speakers whose audio is pushed through a radio chain, for two reasons:
#   1. realism — dispatch genuinely reaches the body camera over a narrow-band
#      radio link, so a full-bandwidth studio voice there sounds wrong;
#   2. distinctness — Andrew (dispatch) and Liam (Morrow) are both adult male
#      voices and measured only ~2.3x apart in timbre, which reads as one man
#      talking to himself. Band-limiting makes the dispatcher unmistakable.
RADIO_SPEAKERS = {"SPEAKER_02"}

# 300-3400 Hz is the classic land-mobile-radio passband; the compressor evens out
# the level the way an actual radio does, and the volume lift compensates for the
# energy thrown away by the band-limiting.
#
# Each filter appears twice because ffmpeg's biquads are 12 dB/octave: a single
# lowpass left a 3.4-5 kHz shoulder that still sounded studio-clean. Cascading
# them to 24 dB/octave took energy above 3.4 kHz from 0.90 % to 0.26 % and
# doubled the measured timbre distance between dispatch and the detective.
RADIO_FILTER_CHAIN = (
    "highpass=f=300,highpass=f=300,lowpass=f=3400,lowpass=f=3400,"
    "acompressor=threshold=0.089:ratio=3:attack=5:release=80,"
    "volume=2.4"
)

# Small, deterministic prosody offsets. Real people do not read a statement at a
# metronomic pace; a fixed rate is a big part of what "sounds like a robot"
# means. Kept tiny (<= 4 %) because larger shifts distort vowels.
RATE_OFFSETS = ("+0%", "-4%", "+3%", "-2%")
# Pitch movement is limited to 0 / +-2 Hz for the same reason.
PITCH_OFFSETS = ("+0Hz", "-2Hz", "+2Hz")

# Which providers to try, in order, for each --engine value. The explicit
# engines still degrade to "tone" so a build never dies mid-run.
ENGINE_CHAINS: dict[str, tuple[str, ...]] = {
    "auto": ("edge", "say", "tone"),
    "edge": ("edge", "tone"),
    "say": ("say", "tone"),
    "tone": ("tone",),
}

DEMO = [
    {
        "slug": "interview-witness-a",
        "filename": "2026-08-14_interview_witness-a.wav",
        "source_type": "interview",
        "recorded_at": "2026-08-14T22:42:00-06:00",
        "noise": False,
        "lines": [
            ("SPEAKER_00", "This is Detective Morrow speaking with witness Carla Nadeau at the Westbrook detachment."),
            ("SPEAKER_00", "It is August fourteenth, twenty twenty six, and the time is ten forty two in the evening."),
            ("SPEAKER_00", "Can you tell me what you saw on Saturday evening near the community centre?"),
            ("SPEAKER_01", "I was walking my dog along Fourth Street when a blue pickup truck pulled up beside the curb."),
            ("SPEAKER_01", "It had a cracked rear window and a silver toolbox in the back."),
            ("SPEAKER_01", "A man got out and walked toward the arena doors, and he was carrying a green duffel bag."),
            ("SPEAKER_00", "Did you get a clear look at his face?"),
            ("SPEAKER_01", "No, he was wearing a hood, and the lot lights were mostly out."),
            ("SPEAKER_01", "He came back about ten minutes later and drove away toward the north."),
        ],
    },
    {
        "slug": "bodycam-unit-412",
        "filename": "2026-08-16_bodycam_unit-412.wav",
        "source_type": "bodycam",
        "recorded_at": "2026-08-16T23:05:00-06:00",
        "noise": False,
        "lines": [
            ("SPEAKER_00", "Four twelve to dispatch, we are arriving at the community centre parking lot."),
            ("SPEAKER_00", "I have a blue pickup truck parked on the north side, the plate is partially obscured."),
            ("SPEAKER_00", "Requesting a registration check on a blue Chevrolet pickup, Ontario plate."),
            ("SPEAKER_02", "Copy four twelve, stand by for the registration result."),
            ("SPEAKER_02", "The vehicle is registered to a garage on Mill Road, no flags on the plate."),
        ],
    },
    {
        "slug": "cassette-statement-1998",
        "filename": "1998-04-02_cassette_statement.wav",
        "source_type": "cassette",
        "recorded_at": "1998-04-02T10:15:00-06:00",
        "noise": True,
        "lines": [
            ("SPEAKER_00", "This recording is a copy of a statement given to Constable Leblanc."),
            ("SPEAKER_01", "The witness described a dark pickup truck with a loud exhaust near the arena."),
            ("SPEAKER_01", "She recalled two men loading boxes into the truck bed."),
            ("SPEAKER_00", "The tape ends abruptly after the vehicle leaves the lot."),
        ],
    },
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def _digest(*parts: str) -> str:
    """Stable hash of the given strings; used for cache keys and prosody."""
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _short_error(exc: BaseException) -> str:
    """One-line, length-capped description of a provider failure."""
    message = " ".join(str(exc).split())
    if len(message) > 90:
        message = message[:87] + "..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _silence_frames(frames: int) -> bytes:
    """Zero-valued s16le samples; gaps are measured in frames to stay exact."""
    return b"\x00\x00" * max(frames, 0)


# --------------------------------------------------------------------------- #
# audio helpers
# --------------------------------------------------------------------------- #

def _beep(duration_s: float, freq: float = 210.0) -> bytes:
    total = max(int(duration_s * SAMPLE_RATE), SAMPLE_RATE // 4)
    fade = min(600, total // 4)
    out = bytearray()
    for i in range(total):
        envelope = min(1.0, i / fade, (total - i) / fade)
        value = int(7000 * envelope * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        out += struct.pack("<h", value)
    return bytes(out)


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"unexpected wav format in {path}")
        return handle.readframes(handle.getnframes())


def _convert_to_pcm(src: Path, dst: Path) -> bytes:
    """Transcode any ffmpeg-readable file to 16 kHz mono s16le PCM bytes."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        check=True, capture_output=True,
    )
    return _read_pcm(dst)


def _radio_filter(pcm: bytes, scratch: Path, index: int) -> bytes:
    """Push PCM through the radio chain, returning filtered PCM of the same shape.

    This runs *inside* the synthesis step, before the caller measures duration,
    so the bytes we time and the bytes we write are the same signal. Filtering
    afterwards would shift every later timestamp.

    Falls back to the unfiltered audio: a missing ffmpeg must not cost us the
    dataset, it only costs us the radio colouring.
    """
    if not _has("ffmpeg"):
        return pcm
    src = scratch / f"line{index}.radio-in.wav"
    dst = scratch / f"line{index}.radio-out.wav"
    with wave.open(str(src), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-af", RADIO_FILTER_CHAIN,
             "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
            check=True, capture_output=True,
        )
        filtered = _read_pcm(dst)
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        print(f"  ! radio filter failed for line {index}: {_short_error(exc)}", file=sys.stderr)
        return pcm
    return filtered or pcm


def _add_noise(src: Path, dst: Path, amplitude: float = 0.05, seed: int = 20260814) -> bool:
    if not _has("ffmpeg"):
        return False
    # The seed is pinned because anoisesrc defaults to a random one, which would
    # make the cassette's checksum change on every run even though the speech is
    # cached and byte-identical.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-f", "lavfi", "-i",
        f"anoisesrc=color=pink:amplitude={amplitude}:sample_rate={SAMPLE_RATE}:seed={seed}",
        "-filter_complex", "amix=inputs=2:duration=first:weights=1 0.35",
        "-ar", str(SAMPLE_RATE), "-ac", "1", str(dst),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
# prosody + pause planning
# --------------------------------------------------------------------------- #

def _prosody(text: str) -> tuple[str, str]:
    """Deterministic (rate, pitch) offsets for one line.

    Derived from a hash of the text rather than ``random`` so the same input
    always requests the same prosody. That is what makes a warm-cache rebuild
    byte-identical; a cold rebuild still varies at the sample level, because the
    remote neural service is not bit-reproducible. Keeping this deterministic
    means any such difference is attributable to the service, not to us.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return RATE_OFFSETS[digest[0] % len(RATE_OFFSETS)], PITCH_OFFSETS[digest[1] % len(PITCH_OFFSETS)]


def _pause_after(text: str, speaker: str, next_speaker: str) -> float:
    """Gap inserted after a line, in seconds.

    Turn boundaries get the longest gap because that is where a real recording
    has an audible breath; a question mark gets a slightly longer beat than a
    plain statement. Everything else keeps the original 0.45 s cadence.
    """
    if next_speaker != speaker:
        return PAUSE_SPEAKER_CHANGE
    if text.rstrip().endswith("?"):
        return PAUSE_AFTER_QUESTION
    return GAP_SECONDS


# --------------------------------------------------------------------------- #
# TTS providers — each raises on failure, never returns empty audio
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Utterance:
    """One line of speech plus the prosody chosen for it."""

    text: str
    speaker: str
    rate: str
    pitch: str
    voice_override: str | None = None


@lru_cache(maxsize=1)
def _say_voice_list() -> tuple[tuple[str, str], ...]:
    """Parse ``say -v '?'`` into (name, locale) pairs, best effort."""
    if not _has("say"):
        return ()
    try:
        proc = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return ()
    voices: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        head = line.split("#", 1)[0].strip()
        if not head:
            continue
        parts = head.split()
        if len(parts) < 2:
            continue
        # Voice names may contain spaces ("Samantha (Enhanced)"); the locale is
        # always the trailing token, e.g. "en_US".
        voices.append((" ".join(parts[:-1]), parts[-1]))
    return tuple(voices)


@lru_cache(maxsize=1)
def _preferred_say_voice() -> str | None:
    """Best installed macOS voice, or None to use the system default."""
    voices = _say_voice_list()
    english = [(name, locale) for name, locale in voices if locale.lower().startswith("en")]
    for marker in ("Premium", "Enhanced"):
        for name, _locale in english:
            if marker in name:
                return name
    return None


def _voice_for(provider: str, speaker: str, override: str | None) -> str | None:
    """Resolve the concrete voice name for a provider/speaker pair."""
    if provider == "edge":
        return override or VOICE_MAP.get(speaker, FALLBACK_EDGE_VOICE)
    if provider == "say":
        return override or _preferred_say_voice()
    return None  # tone bursts are voiceless


def _edge_provider(utt: Utterance, voice: str | None, scratch: Path, index: int) -> bytes:
    """Neural speech via edge-tts: MP3 out, transcoded to our PCM format."""
    import edge_tts  # imported lazily: --engine say must work without the package

    mp3 = scratch / f"line{index}.edge.mp3"
    wav = scratch / f"line{index}.edge.wav"

    async def _save() -> None:
        communicate = edge_tts.Communicate(utt.text, voice or FALLBACK_EDGE_VOICE,
                                           rate=utt.rate, pitch=utt.pitch)
        await asyncio.wait_for(communicate.save(str(mp3)), timeout=EDGE_TIMEOUT_S)

    asyncio.run(_save())
    if not mp3.exists() or mp3.stat().st_size == 0:
        raise RuntimeError("edge-tts returned no audio")
    return _convert_to_pcm(mp3, wav)


def _say_provider(utt: Utterance, voice: str | None, scratch: Path, index: int) -> bytes:
    """macOS say. Uses the enhanced voice when one is installed."""
    if not _has("say"):
        raise RuntimeError("say not found")
    aiff = scratch / f"line{index}.aiff"
    wav = scratch / f"line{index}.say.wav"
    cmd = ["say", "-o", str(aiff)]
    if voice:
        cmd += ["-v", voice]
    cmd.append(utt.text)
    subprocess.run(cmd, check=True, capture_output=True)
    return _convert_to_pcm(aiff, wav)


def _tone_provider(utt: Utterance, voice: str | None, scratch: Path, index: int) -> bytes:
    """Voiceless fallback: a beep as long as the line would take to read."""
    words = max(len(utt.text.split()), 3)
    return _beep(0.32 * words + 0.4)


PROVIDERS = {
    "edge": _edge_provider,
    "say": _say_provider,
    "tone": _tone_provider,
}


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

def _cache_file(cache_dir: Path, provider: str, utt: Utterance, voice: str | None) -> Path:
    """Cache path for one rendered line.

    The provider is part of the key so a ``say`` render can never be mistaken
    for an ``edge`` render of the same text.
    """
    key = _digest(provider, utt.text, voice or "", utt.rate, utt.pitch)
    return cache_dir / f"{key}.pcm"


def _cache_read(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _cache_write(path: Path, provider: str, utt: Utterance, voice: str | None, pcm: bytes) -> None:
    """Persist a rendered line; failures are non-fatal (the cache is optional)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pcm.tmp")
        tmp.write_bytes(pcm)
        tmp.replace(path)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "provider": provider,
                    "voice": voice,
                    "rate": utt.rate,
                    "pitch": utt.pitch,
                    "sample_rate": SAMPLE_RATE,
                    "frames": len(pcm) // 2,
                    "text": utt.text,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  ! could not write TTS cache: {_short_error(exc)}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# synthesis with fallback
# --------------------------------------------------------------------------- #

def _synthesise(utt: Utterance, chain: tuple[str, ...], scratch: Path, index: int,
                cache_dir: Path) -> tuple[bytes, str]:
    """Render one line, walking the provider chain until something works.

    Returns the PCM bytes and the provider that produced them. ``tone`` is
    always last in every chain and cannot fail, so this never raises for
    provider reasons.

    Radio speakers are filtered here, before the caller measures anything, so
    the returned bytes are what gets timed and written. The filter is applied on
    the way out of the cache too, which keeps the cache itself provider-neutral.
    """
    skipped: list[str] = []
    for provider in chain:
        voice = _voice_for(provider, utt.speaker, utt.voice_override)
        cached = _cache_file(cache_dir, provider, utt, voice)
        hit = _cache_read(cached)
        if hit is not None:
            _report(index, provider, skipped, cached=True)
            return _maybe_radio(hit, utt, provider, scratch, index), provider
        try:
            pcm = _render_with_retry(provider, utt, voice, scratch, index)
        except Exception as exc:  # noqa: BLE001 - every provider must fail soft
            skipped.append(f"{provider} ({_short_error(exc)})")
            continue
        if not pcm:
            skipped.append(f"{provider} (no audio)")
            continue
        _cache_write(cached, provider, utt, voice, pcm)
        _report(index, provider, skipped, cached=False)
        return _maybe_radio(pcm, utt, provider, scratch, index), provider
    raise RuntimeError(f"no TTS provider produced audio for line {index}")


def _render_with_retry(provider: str, utt: Utterance, voice: str | None, scratch: Path,
                       index: int) -> bytes:
    """Render one line with one provider, retrying it before letting the chain step down.

    Only ``edge`` is retried. Stepping down the chain is audible — it drops a
    different voice into the middle of a recording — so a transient
    ``NoAudioReceived`` must not be treated as a verdict. ``ImportError`` and
    ``FileNotFoundError`` are excluded from the retry because the package or the
    binary is simply absent: four attempts would add ~14 s of sleeps per line to
    a build that is going to fall back regardless.
    """
    render = PROVIDERS[provider]
    if provider not in RETRYABLE_PROVIDERS:
        return render(utt, voice, scratch, index)

    last: BaseException | None = None
    for attempt in range(1, EDGE_ATTEMPTS + 1):
        try:
            time.sleep(EDGE_REQUEST_PAUSE_S)
            pcm = render(utt, voice, scratch, index)
        except PERMANENT_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001 - treated as transient, then retried
            last = exc
        else:
            if pcm:
                return pcm
            # Empty output is the same symptom as an exception (edge-tts writes
            # a zero-byte MP3 under throttling), so it gets the same treatment.
            last = RuntimeError(f"{provider} returned no audio")
        if attempt < EDGE_ATTEMPTS:
            time.sleep(EDGE_BACKOFF_S * (2 ** (attempt - 1)))

    # Exhausted every attempt: the next provider *will* sound different, so say
    # so on stderr rather than letting a mixed-voice recording ship silently.
    print(
        f"  ! line {index}: {provider} failed {EDGE_ATTEMPTS}x "
        f"({_short_error(last) if last else 'no audio'}) — "
        f"falling back, this recording will mix voices",
        file=sys.stderr,
    )
    raise last if last is not None else RuntimeError(f"{provider} produced no audio")


def _maybe_radio(pcm: bytes, utt: Utterance, provider: str, scratch: Path,
                 index: int) -> bytes:
    """Apply the radio chain for radio speakers, never for tone bursts."""
    if utt.speaker not in RADIO_SPEAKERS:
        return pcm
    if provider == "tone":
        return pcm  # a beep through a radio filter is still just a beep
    return _radio_filter(pcm, scratch, index)


def _report(index: int, provider: str, skipped: list[str], cached: bool) -> None:
    """One short stderr line whenever the chain had to fall through."""
    if not skipped:
        return
    suffix = ", cached" if cached else ""
    print(f"  · line {index}: used {provider}{suffix} after {'; '.join(skipped)}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# transcript helpers
# --------------------------------------------------------------------------- #

def _word_timings(text: str, start: float, duration: float) -> list[dict]:
    words = text.split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    cursor = start
    timings = []
    for word, weight in zip(words, weights):
        span = duration * weight / total
        timings.append({"w": word, "start": round(cursor, 3), "end": round(cursor + span, 3)})
        cursor += span
    return timings


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build_recording(spec: dict, out_dir: Path, engine: str, voice: str | None,
                    cache_dir: Path) -> dict:
    scratch = out_dir / f".scratch-{spec['slug']}"
    scratch.mkdir(parents=True, exist_ok=True)

    chain = ENGINE_CHAINS[engine]
    lines: list[tuple[str, str]] = spec["lines"]

    pcm = bytearray()
    segments = []
    cursor_frames = 0  # integer frame cursor keeps timestamps frame-exact
    provider_counts: dict[str, int] = {}

    for index, (speaker, text) in enumerate(lines):
        next_speaker = lines[index + 1][0] if index + 1 < len(lines) else speaker
        rate, pitch = _prosody(text)
        utterance = Utterance(text=text, speaker=speaker, rate=rate, pitch=pitch,
                              voice_override=voice)
        clip, provider = _synthesise(utterance, chain, scratch, index, cache_dir)
        provider_counts[provider] = provider_counts.get(provider, 0) + 1

        clip_frames = len(clip) // 2
        duration = clip_frames / SAMPLE_RATE
        start = cursor_frames / SAMPLE_RATE
        end_frames = cursor_frames + clip_frames

        segments.append(
            {
                "start": round(start, 3),
                "end": round(end_frames / SAMPLE_RATE, 3),
                "text": text,
                "speaker": speaker,
                # Provenance per line, not just in aggregate: this is what lets
                # a mixed-voice recording be detected after the fact. The runtime
                # sidecar reader ignores unknown segment keys, so this is additive.
                "tts_provider": provider,
                "words": _word_timings(text, start, duration),
            }
        )

        pcm += clip
        pause_frames = round(_pause_after(text, speaker, next_speaker) * SAMPLE_RATE)
        pcm += _silence_frames(pause_frames)
        cursor_frames = end_frames + pause_frames

    raw_wav = scratch / "raw.wav"
    with wave.open(str(raw_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(pcm))

    target = out_dir / spec["filename"]
    if spec.get("noise"):
        if not _add_noise(raw_wav, target):
            shutil.copyfile(raw_wav, target)
    else:
        shutil.copyfile(raw_wav, target)

    sidecar = target.with_suffix(".transcript.json")
    sidecar.write_text(
        json.dumps(
            {
                "language": "en",
                "source": "synthetic-demo",
                "note": "Reference transcript generated from the scripted lines; "
                        "timestamps are exact because each line was synthesised separately.",
                # Voice provenance, persisted rather than only printed: a build
                # that fell back mid-recording is audibly mixed, and that fact
                # must survive the build. Without this it is undetectable later.
                "tts": {
                    "providers": dict(sorted(provider_counts.items())),
                    "mixed": len(provider_counts) > 1,
                },
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
        "source_type": spec["source_type"],
        "recorded_at": spec["recorded_at"],
        "duration_s": round(cursor_frames / SAMPLE_RATE, 2),
        "segments": len(segments),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "providers": provider_counts,
    }


def upload(base_url: str, entry: dict) -> str | None:
    if not _has("curl"):
        print("  ! curl not available; skipping upload", file=sys.stderr)
        return None
    cmd = [
        "curl", "-sS", "-X", "POST", f"{base_url.rstrip('/')}/api/recordings",
        "-H", "X-Actor: demo.curator", "-H", "X-Role: admin",
        "-F", f"file=@{entry['audio']}",
        "-F", f"source_type={entry['source_type']}",
        "-F", f"recorded_at={entry['recorded_at']}",
        "-F", f"transcript_file=@{entry['sidecar']};type=application/json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  ! upload failed: {proc.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout).get("id")
    except json.JSONDecodeError:
        print(f"  ! unexpected response: {proc.stdout[:200]}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _list_voices() -> int:
    print("casting (VOICE_MAP, edge-tts):")
    for speaker, voice in VOICE_MAP.items():
        print(f"  {speaker}: {voice}")

    print("\nedge-tts English voices:")
    try:
        import edge_tts

        voices = asyncio.run(edge_tts.list_voices())
        for item in sorted(voices, key=lambda v: v["ShortName"]):
            if item["ShortName"].startswith("en-"):
                print(f"  {item['ShortName']:<34} {item['Gender']:<7} {item.get('Locale', '')}")
    except Exception as exc:  # noqa: BLE001 - listing is a convenience command
        print(f"  ! could not query edge-tts: {_short_error(exc)}", file=sys.stderr)

    print("\nmacOS say voices (Premium/Enhanced preferred):")
    premium = [name for name, _locale in _say_voice_list() if "Premium" in name or "Enhanced" in name]
    if premium:
        print("  " + ", ".join(premium))
    else:
        print("  none installed — `say` will use the default compact voice")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        # Keep the module docstring's line breaks so the usage examples in
        # --help stay readable instead of collapsing into one paragraph.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "samples"))
    parser.add_argument("--upload", default=None, help="API base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--voice", default=None,
                        help="force one voice for every line (edge voice name or macOS say voice)")
    parser.add_argument("--engine", default="auto", choices=sorted(ENGINE_CHAINS),
                        help="TTS provider chain (default: auto = edge, then say, then tone)")
    parser.add_argument("--list-voices", action="store_true",
                        help="print the casting map and installed voices, then exit")
    args = parser.parse_args()

    if args.list_voices:
        return _list_voices()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / ".tts-cache"

    print(f"output: {out_dir}")
    print(f"engine: {args.engine} -> {' -> '.join(ENGINE_CHAINS[args.engine])}")
    print(f"cache:  {cache_dir}")
    if args.voice:
        print(f"voice:  {args.voice} (forced for every line)")
    else:
        if "edge" in ENGINE_CHAINS[args.engine]:
            casting = ", ".join(f"{speaker}={voice}" for speaker, voice in VOICE_MAP.items())
            print(f"voices: {casting}")
        if "say" in ENGINE_CHAINS[args.engine]:
            print(f"say:    {_preferred_say_voice() or 'system default (no Premium/Enhanced installed)'}")
    if not _has("ffmpeg"):
        print("warning: ffmpeg not found — noise layer and conversion will be skipped")

    manifest = []
    for spec in DEMO:
        print(f"- {spec['slug']} ...", end=" ", flush=True)
        entry = build_recording(spec, out_dir, args.engine, args.voice, cache_dir)
        providers = ", ".join(f"{name} x{count}" for name, count in sorted(entry["providers"].items()))
        print(f"{entry['duration_s']}s, {entry['segments']} segments [{providers}]")
        manifest.append(entry)

    # The manifest keeps its original keys; provider usage is reported on stdout
    # only, so downstream consumers do not see a schema change.
    public_manifest = [{k: v for k, v in entry.items() if k != "providers"} for entry in manifest]
    (out_dir / "manifest.json").write_text(json.dumps(public_manifest, indent=2), encoding="utf-8")

    if args.upload:
        print(f"\nuploading to {args.upload}")
        for entry in manifest:
            recording_id = upload(args.upload, entry)
            print(f"- {entry['slug']}: {recording_id or 'FAILED'}")

    print("\ndone. sample queries to try:")
    print('  "blue pickup truck near a community centre"')
    print('  "registration check"')
    print('  "green duffel bag"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
