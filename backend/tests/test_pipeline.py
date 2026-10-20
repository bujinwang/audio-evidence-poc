"""FR-02 / FR-03: processing stages, timestamps, dependency reporting, retry."""

from __future__ import annotations

from conftest import ADMIN_HEADERS, REVIEWER_HEADERS, upload_ok


def test_segments_have_usable_timestamps(client, witness_sample):
    data = upload_ok(client, witness_sample)
    transcript = client.get(f"/api/recordings/{data['id']}/transcript").json()

    assert transcript["engine"] == "sidecar-reference"
    assert transcript["segment_count"] == len(transcript["segments"]) == 5

    previous_end = -1.0
    for segment in transcript["segments"]:
        assert segment["start_s"] >= 0
        assert segment["end_s"] > segment["start_s"], "every segment needs a positive duration"
        assert segment["start_s"] >= previous_end - 0.001, "segments must be ordered"
        previous_end = segment["end_s"]
        assert segment["words"], "reference transcript supplies word timestamps"
        assert segment["ai_text"] == segment["text"]
        assert segment["review_status"] == "ai_generated"

    assert transcript["segments"][0]["start_s"] == 0.0
    assert transcript["segments"][-1]["end_s"] <= data["duration_s"] + 0.5


def test_denoiser_unavailable_is_reported_not_fatal(client, witness_sample):
    """DENOISER=none in the test env — the job must complete and say why."""
    data = upload_ok(client, witness_sample)

    denoise = data["processing_meta"]["denoise"]
    assert denoise["status"] == "skipped"
    assert "DENOISER=none" in denoise["reason"]
    assert data["status"] == "ready"

    comparison = client.get(f"/api/recordings/{data['id']}/comparison").json()
    assert comparison["available"] is False
    assert "Denoised derivative not produced" in comparison["reason"]


def test_transcription_unavailable_is_reported_not_fatal(client, witness_sample, set_env):
    set_env(TRANSCRIBER="none")
    audio_path, sidecar_path = witness_sample
    response = client.post(
        "/api/recordings",
        files={"file": (audio_path.name, audio_path.read_bytes(), "audio/wav")},
        data={"source_type": "interview"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201
    data = response.json()

    assert data["status"] == "ready"
    assert data["segment_count"] == 0
    assert data["processing_meta"]["transcription"]["status"] == "unavailable"
    assert data["warnings"], "unavailable dependencies must be surfaced to the user"
    assert data["latest_job"]["status"] == "succeeded"
    assert data["latest_job"]["stage"] == "completed_with_warnings"


def test_retry_is_allowed_before_any_review(client, witness_sample):
    data = upload_ok(client, witness_sample)
    response = client.post(f"/api/recordings/{data['id']}/retry", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    refreshed = response.json()
    assert refreshed["status"] == "ready"
    assert refreshed["segment_count"] == 5


def test_retry_is_blocked_once_a_segment_was_reviewed(client, witness_sample):
    data = upload_ok(client, witness_sample)
    transcript = client.get(f"/api/recordings/{data['id']}/transcript").json()
    segment_id = transcript["segments"][0]["id"]

    edit = client.patch(
        f"/api/segments/{segment_id}",
        json={"text": "Corrected opening line.", "note": "review"},
        headers=REVIEWER_HEADERS,
    )
    assert edit.status_code == 200

    response = client.post(f"/api/recordings/{data['id']}/retry", headers=ADMIN_HEADERS)
    assert response.status_code == 409
    assert "would discard corrections" in response.json()["detail"]


def test_retry_requires_admin(client, witness_sample):
    data = upload_ok(client, witness_sample)
    response = client.post(
        f"/api/recordings/{data['id']}/retry",
        headers={"X-Actor": "inv", "X-Role": "investigator"},
    )
    assert response.status_code == 403


def test_level_matching_brings_a_quiet_derivative_up_to_the_source(tmp_path):
    """DeepFilterNet attenuates its output; the derivative must be brought back up.

    Measured on AMI distant-microphone audio the raw filter output sat at 27% of
    the input RMS, and the same clip scored 95.2% WER denoised versus 72.8% once
    the level was restored — the filtering was unchanged. Without this correction
    every denoised comparison in the app measures a level drop, not the denoising.

    The unit under test is `_match_level`, not the filter: a synthetic signal is
    not speech, so DeepFilterNet legitimately suppresses it and the resulting
    level says nothing about the correction.
    """
    import subprocess

    import pytest

    from app.pipeline.denoise import _match_level, _mean_volume_db

    def make(path, gain_db):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=16000",
             "-af", f"volume={gain_db}dB", "-ac", "1", str(path)],
            check=True,
        )

    reference = tmp_path / "reference.flac"
    quiet = tmp_path / "quiet.flac"
    make(reference, 0)
    make(quiet, -18)

    before = _mean_volume_db(quiet)
    gain = _match_level(reference, quiet)
    after = _mean_volume_db(quiet)

    assert gain > 0, "a quieter derivative needs a positive gain"
    assert before is not None and after is not None
    assert after - before == pytest.approx(gain, abs=0.5)
    assert abs(after - _mean_volume_db(reference)) < 1.0

    # Already-matched input is left untouched.
    assert _match_level(reference, reference) == 0.0


# --------------------------------------------------------------------------- #
# voice provenance
# --------------------------------------------------------------------------- #

def test_sidecar_voice_provenance_reaches_the_api(client, tmp_path):
    """A mixed-voice synthetic recording must be detectable through the API.

    The generator can fall back from a neural voice to a compact system voice
    mid-recording, which is audible but leaves no trace in the audio itself.
    It records the condition in the sidecar, and this checks that survives all
    the way into the transcript the reviewer actually sees.
    """
    import json

    from conftest import write_sample

    audio_path, sidecar_path = write_sample(
        tmp_path / "media", "mixed", [("SPEAKER_00", "one line of speech here")]
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["source"] = "synthetic-demo"
    payload["tts"] = {"providers": {"edge": 1, "say": 1}, "mixed": True}
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    response = client.post(
        "/api/recordings",
        files={
            "file": (audio_path.name, audio_path.read_bytes(), "audio/wav"),
            "transcript_file": (sidecar_path.name, sidecar_path.read_bytes(), "application/json"),
        },
        data={"source_type": "interview"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    recording_id = response.json()["id"]

    transcript = client.get(f"/api/recordings/{recording_id}/transcript").json()
    assert transcript["settings"]["tts"] == {"providers": {"edge": 1, "say": 1}, "mixed": True}
    assert transcript["settings"]["source_kind"] == "synthetic-demo"


def test_a_plain_sidecar_reports_no_voice_mixing(client, witness_sample):
    """No tts block in the sidecar means nothing to claim — not a false alarm."""
    data = upload_ok(client, witness_sample)
    transcript = client.get(f"/api/recordings/{data['id']}/transcript").json()

    assert "tts" not in transcript["settings"]
    assert transcript["settings"]["provider"] == "sidecar"
