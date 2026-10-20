"""The TTS layer of the dataset generators.

These generators had no test coverage at all, which is how the defect this file
guards against survived: `make_demo_data._synthesise` walked its provider chain
and stepped down on the *first* exception, so a throttled ``edge-tts`` request
spliced a compact macOS ``say`` voice into the middle of a neural recording.
The result is audibly wrong and — before this file — silently so.

The tests run with no network. Every provider is monkeypatched, so they assert
on the *policy* (how many attempts, in what order, with what sleeps) rather than
on a voice service being reachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import make_demo_data as demo  # noqa: E402
import make_noisy_test_set as noisy  # noqa: E402


PCM = b"\x01\x02" * 160  # 10 ms of non-empty s16le


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    directory = tmp_path / "scratch"
    directory.mkdir()
    return directory


@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    """Record the sleeps instead of taking them, so retries stay instant."""
    sleeps: list[float] = []
    monkeypatch.setattr(demo.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def _backoff_sleeps(sleeps: list[float]) -> list[float]:
    """Only the retry backoffs — the pacing pause is not a retry.

    Every attempt is preceded by ``EDGE_REQUEST_PAUSE_S`` to avoid triggering
    throttling at all, so that value is present even on a first-try success.
    These tests care about backoff, which should appear only between attempts.
    """
    return [s for s in sleeps if s != demo.EDGE_REQUEST_PAUSE_S]


def _utterance(text: str = "the blue pickup truck", speaker: str = "SPEAKER_00") -> demo.Utterance:
    return demo.Utterance(text=text, speaker=speaker, rate="+0%", pitch="+0Hz")


def _providers(monkeypatch, **behaviour) -> dict[str, list[int]]:
    """Replace every provider with a stub; ``behaviour`` maps name -> outcome.

    ``"ok"`` returns PCM, ``"fail"`` raises, ``"empty"`` returns b"". Each stub
    records calls so tests can assert on attempt counts.
    """
    calls: dict[str, list[int]] = {}
    for name in demo.PROVIDERS:
        calls[name] = []
        outcome = behaviour.get(name, "ok")

        def make(name=name, outcome=outcome):
            def _stub(utt, voice, scratch_dir, index):
                calls[name].append(index)
                if outcome == "fail":
                    raise RuntimeError("NoAudioReceived")
                if outcome == "empty":
                    return b""
                return PCM
            return _stub

        monkeypatch.setitem(demo.PROVIDERS, name, make())
    return calls


# --------------------------------------------------------------------------- #
# retry policy
# --------------------------------------------------------------------------- #

def test_transient_failure_is_retried_before_the_chain_steps_down(
    monkeypatch, no_sleep, scratch
):
    """The defect itself: one throttle must not cost a voice change."""
    attempts = {"n": 0}

    def flaky(utt, voice, scratch_dir, index):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("NoAudioReceived")
        return PCM

    monkeypatch.setitem(demo.PROVIDERS, "edge", flaky)
    monkeypatch.setitem(demo.PROVIDERS, "say", lambda *a: pytest.fail("must not step down"))
    monkeypatch.setitem(demo.PROVIDERS, "tone", lambda *a: pytest.fail("must not step down"))

    pcm, provider = demo._synthesise(_utterance(), ("edge", "say", "tone"), scratch, 0,
                                     scratch / "cache")

    assert provider == "edge"
    assert pcm
    assert attempts["n"] == 3


def test_retries_are_bounded_and_the_error_surfaces(monkeypatch, no_sleep, scratch):
    calls = _providers(monkeypatch, edge="fail")

    with pytest.raises(RuntimeError):
        demo._render_with_retry("edge", _utterance(), None, scratch, 0)

    assert len(calls["edge"]) == demo.EDGE_ATTEMPTS


def test_exhaustion_warns_loudly_about_the_mixed_voice(monkeypatch, capfd, no_sleep, scratch):
    """A fallback is audible, so it must be visible in the build log."""
    _providers(monkeypatch, edge="fail")

    with pytest.raises(RuntimeError):
        demo._render_with_retry("edge", _utterance(), None, scratch, 7)

    err = capfd.readouterr().err
    assert "line 7" in err
    assert "mix voices" in err


def test_backoff_is_exponential_and_skips_the_final_attempt(monkeypatch, no_sleep, scratch):
    """4 attempts means 3 sleeps: no point waiting after the last failure."""
    _providers(monkeypatch, edge="fail")

    with pytest.raises(RuntimeError):
        demo._render_with_retry("edge", _utterance(), None, scratch, 0)

    # The pause before each attempt, then the growing backoff.
    assert no_sleep.count(demo.EDGE_BACKOFF_S * 1) == 1
    assert no_sleep.count(demo.EDGE_BACKOFF_S * 2) == 1
    assert no_sleep.count(demo.EDGE_BACKOFF_S * 4) == 1


def test_empty_output_is_retried_like_an_exception(monkeypatch, no_sleep, scratch):
    """edge-tts writes a zero-byte MP3 when throttled; that is the same symptom."""
    attempts = {"n": 0}

    def empty_then_ok(utt, voice, scratch_dir, index):
        attempts["n"] += 1
        return b"" if attempts["n"] < 2 else PCM

    monkeypatch.setitem(demo.PROVIDERS, "edge", empty_then_ok)
    pcm = demo._render_with_retry("edge", _utterance(), None, scratch, 0)

    assert pcm == PCM
    assert attempts["n"] == 2


# --------------------------------------------------------------------------- #
# what must NOT be retried
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("provider", ["say", "tone"])
def test_deterministic_providers_are_not_retried(monkeypatch, no_sleep, scratch, provider):
    """`say`/`tone` fail the same way every time; retrying only burns wall-clock."""
    calls = _providers(monkeypatch, **{provider: "fail"})

    with pytest.raises(RuntimeError):
        demo._render_with_retry(provider, _utterance(), None, scratch, 0)

    assert len(calls[provider]) == 1
    assert no_sleep == []


@pytest.mark.parametrize("error", [ImportError("no edge_tts"), FileNotFoundError("ffmpeg")])
def test_absent_dependencies_fail_fast(monkeypatch, no_sleep, scratch, error):
    """A missing package or binary cannot be fixed by waiting ~14 seconds."""
    calls: list[int] = []

    def missing(utt, voice, scratch_dir, index):
        calls.append(index)
        raise error

    monkeypatch.setitem(demo.PROVIDERS, "edge", missing)

    with pytest.raises(type(error)):
        demo._render_with_retry("edge", _utterance(), None, scratch, 0)

    assert len(calls) == 1
    # Fail-fast means no *backoff*; the one pacing pause before the attempt is
    # expected and is not a retry.
    assert _backoff_sleeps(no_sleep) == []


def test_a_chain_without_edge_never_sleeps(monkeypatch, no_sleep, scratch):
    """`--engine say` and `--engine tone` must stay instant."""
    calls = _providers(monkeypatch, say="ok", tone="ok")

    pcm, provider = demo._synthesise(_utterance(), demo.ENGINE_CHAINS["say"], scratch, 0,
                                     scratch / "cache")

    assert provider == "say"
    assert calls["tone"] == []
    # Not merely no backoff — no pacing pause either, because no provider in
    # this chain is retryable.
    assert no_sleep == []


# --------------------------------------------------------------------------- #
# cache integrity
# --------------------------------------------------------------------------- #

def test_a_cached_line_is_served_without_any_network_attempt(monkeypatch, no_sleep, scratch):
    """A cache hit is not a failure; a warm rebuild must not sleep or re-render."""
    _providers(monkeypatch, edge="ok")
    cache = scratch / "cache"
    utt = _utterance()

    first, provider = demo._synthesise(utt, ("edge", "say"), scratch, 0, cache)
    assert provider == "edge"

    calls = _providers(monkeypatch, edge="fail")  # provider would now fail
    second, provider = demo._synthesise(utt, ("edge", "say"), scratch, 0, cache)

    assert second == first
    assert provider == "edge"
    assert calls["edge"] == []  # never called: served from cache
    # Line 0 was rendered cold first (one pacing pause, no backoff). The warm
    # call reached no provider at all, so it added no backoff either.
    assert _backoff_sleeps(no_sleep) == []


def test_cache_entries_are_keyed_per_provider(monkeypatch, scratch):
    """A `say` render must never be mistaken for an `edge` render of the same text."""
    utt = _utterance()
    edge_key = demo._cache_file(scratch, "edge", utt, "en-CA-LiamNeural")
    say_key = demo._cache_file(scratch, "say", utt, "Samantha")
    same_voice_say = demo._cache_file(scratch, "say", utt, "en-CA-LiamNeural")

    assert len({edge_key, say_key, same_voice_say}) == 3


def test_voice_change_invalidates_the_cache(monkeypatch, scratch):
    utt = _utterance()
    a = demo._cache_file(scratch, "edge", utt, "en-CA-LiamNeural")
    b = demo._cache_file(scratch, "edge", utt, "en-CA-ClaraNeural")
    assert a != b


# --------------------------------------------------------------------------- #
# the noisy generator shares the fix
# --------------------------------------------------------------------------- #

def test_noisy_generator_delegates_to_the_shared_retry(monkeypatch, no_sleep, scratch):
    """It must not re-implement the retry, or the loops nest 4x4 = 16 attempts."""
    calls: list[tuple] = []
    real = demo._synthesise

    def spy(utt, chain, scratch_dir, index, cache_dir):
        calls.append((chain, index))
        return real(utt, chain, scratch_dir, index, cache_dir)

    monkeypatch.setattr(demo, "_synthesise", spy)
    _providers(monkeypatch, edge="ok")

    pcm, provider = noisy.synthesise_line(_utterance(), ("edge", "say", "tone"), scratch, 0,
                                          scratch / "cache")

    assert provider == "edge"
    assert pcm
    assert len(calls) == 1  # exactly one walk of the chain, not nested
    assert calls[0][0] == ("edge", "say", "tone")


def test_noisy_generator_retries_transient_failures(monkeypatch, no_sleep, scratch):
    attempts = {"n": 0}

    def flaky(utt, voice, scratch_dir, index):
        attempts["n"] += 1
        if attempts["n"] < demo.EDGE_ATTEMPTS:
            raise RuntimeError("NoAudioReceived")
        return PCM

    monkeypatch.setitem(demo.PROVIDERS, "edge", flaky)
    monkeypatch.setitem(demo.PROVIDERS, "say", lambda *a: pytest.fail("must not step down"))

    _, provider = noisy.synthesise_line(_utterance(), ("edge", "say"), scratch, 0,
                                        scratch / "cache")

    assert provider == "edge"
    assert attempts["n"] == demo.EDGE_ATTEMPTS


def test_noisy_generator_has_no_private_retry_loop_left():
    """Guard against the duplicated constants coming back."""
    for name in ("EDGE_ATTEMPTS", "EDGE_REQUEST_DELAY_S", "EDGE_BACKOFF_S"):
        assert not hasattr(noisy, name), f"{name} should live in make_demo_data only"


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #

def test_sidecar_records_per_line_and_aggregate_provenance(tmp_path, monkeypatch):
    """A mixed-voice recording must be detectable *after* the build."""
    out = tmp_path / "out"
    out.mkdir()
    spec = {
        "slug": "provenance",
        "filename": "provenance.wav",
        "source_type": "interview",
        "recorded_at": "2026-08-14T22:42:00-06:00",
        "noise": False,
        "lines": [
            ("SPEAKER_00", "first line of the recording"),
            ("SPEAKER_01", "second line of the recording"),
        ],
    }

    # Line 0 succeeds, line 1 falls back — the exact mixed-voice case.
    seen: list[int] = []

    def edge(utt, voice, scratch_dir, index):
        seen.append(index)
        if index == 1:
            raise RuntimeError("NoAudioReceived")
        return PCM

    monkeypatch.setitem(demo.PROVIDERS, "edge", edge)
    monkeypatch.setitem(demo.PROVIDERS, "say", lambda *a: PCM)
    monkeypatch.setitem(demo.PROVIDERS, "tone", lambda *a: PCM)
    monkeypatch.setattr(demo.time, "sleep", lambda _s: None)
    # The radio filter and noise layer shell out to ffmpeg; neither applies here.
    monkeypatch.setattr(demo, "_maybe_radio", lambda pcm, *a, **k: pcm)

    entry = demo.build_recording(spec, out, "auto", None, out / ".tts-cache")

    import json

    payload = json.loads(Path(entry["sidecar"]).read_text(encoding="utf-8"))
    per_line = [seg["tts_provider"] for seg in payload["segments"]]
    assert per_line == ["edge", "say"]
    assert payload["tts"] == {"providers": {"edge": 1, "say": 1}, "mixed": True}
    assert len(seen) == demo.EDGE_ATTEMPTS + 1  # line 0 once, line 1 retried


def test_a_clean_recording_is_marked_unmixed(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    spec = {
        "slug": "clean",
        "filename": "clean.wav",
        "source_type": "interview",
        "recorded_at": "2026-08-14T22:42:00-06:00",
        "noise": False,
        "lines": [("SPEAKER_00", "only line of the recording")],
    }
    monkeypatch.setitem(demo.PROVIDERS, "edge", lambda *a: PCM)
    monkeypatch.setattr(demo, "_maybe_radio", lambda pcm, *a, **k: pcm)

    entry = demo.build_recording(spec, out, "auto", None, out / ".tts-cache")

    import json

    payload = json.loads(Path(entry["sidecar"]).read_text(encoding="utf-8"))
    assert payload["tts"] == {"providers": {"edge": 1}, "mixed": False}
