"""End-to-end acceptance walk-through (PRD section 11 and 13).

This is the test that maps one-to-one onto the demo script: upload three
recordings, search in natural language, jump to the timestamp, correct a
transcription error, verify the segment, and read the audit trail.
"""

from __future__ import annotations

import hashlib
import time

from conftest import ADMIN_HEADERS, REVIEWER_HEADERS, upload_ok

from app.config import get_settings


def test_acceptance_workflow(client, witness_sample, bodycam_sample, cassette_sample):
    # 1. three recordings ingest successfully
    witness = upload_ok(client, witness_sample, source_type="interview")
    bodycam = upload_ok(client, bodycam_sample, source_type="bodycam")
    cassette = upload_ok(client, cassette_sample, source_type="cassette")
    for recording in (witness, bodycam, cassette):
        assert recording["status"] == "ready", recording
        assert recording["segment_count"] > 0

    # 2. the original is byte-identical to what was uploaded
    stored = get_settings().originals_dir / witness["id"] / witness_sample[0].name
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == witness["sha256"]

    # 6. natural-language search returns candidate passages, fast
    started = time.perf_counter()
    results = client.get(
        "/api/search",
        params={"q": "blue pickup truck near a community centre", "mode": "hybrid"},
    ).json()
    elapsed = time.perf_counter() - started

    assert results["total"] >= 2, results
    assert elapsed < 2.0, f"search took {elapsed:.2f}s — target is well under a second"
    assert {hit["recording_id"] for hit in results["hits"]} >= {witness["id"], bodycam["id"]}

    # 7. a result opens the original audio at the relevant timestamp
    hit = next(h for h in results["hits"] if h["recording_id"] == witness["id"])
    ranged = client.get(
        f"/api/recordings/{hit['recording_id']}/audio",
        headers={**ADMIN_HEADERS, "Range": "bytes=0-2047"},
    )
    assert ranged.status_code == 206
    assert ranged.headers["content-range"].startswith("bytes 0-2047/")
    assert ranged.headers["accept-ranges"] == "bytes"
    assert len(ranged.content) == 2048

    transcript = client.get(f"/api/recordings/{hit['recording_id']}/transcript").json()
    segment = next(s for s in transcript["segments"] if s["id"] == hit["segment_id"])
    assert segment["start_s"] == hit["start_s"], "hit timestamp must match the transcript segment"

    # 8. a reviewer corrects the segment
    original_ai_text = segment["ai_text"]
    corrected = client.patch(
        f"/api/segments/{segment['id']}",
        json={
            "text": original_ai_text.replace("curb", "curb, near the arena entrance"),
            "note": "added detail audible at 00:12",
        },
        headers=REVIEWER_HEADERS,
    )
    assert corrected.status_code == 200
    assert corrected.json()["review_status"] == "corrected"

    # 9. the AI text and the revision survive
    history = client.get(f"/api/segments/{segment['id']}/history").json()
    assert history["ai_text"] == original_ai_text
    assert history["current_text"] != original_ai_text
    assert history["revisions"][0]["prev_text"] == original_ai_text

    # 10. explicit verification
    verified = client.post(
        f"/api/segments/{segment['id']}/verify",
        json={"verified": True, "note": "listened twice"},
        headers=REVIEWER_HEADERS,
    )
    assert verified.status_code == 200
    assert verified.json()["review_status"] == "verified"

    # 11. the audit trail records it
    events = client.get(f"/api/audit?recording_id={witness['id']}", headers=ADMIN_HEADERS).json()
    actions = [event["action"] for event in events]
    for expected in ("recording.uploaded", "segment.edited", "segment.verified"):
        assert expected in actions

    verified_event = next(e for e in events if e["action"] == "segment.verified")
    assert verified_event["actor"] == "test.reviewer"
    assert verified_event["detail"]["note"] == "listened twice"

    # the recording now reports one verified segment out of five
    refreshed = client.get(f"/api/recordings/{witness['id']}").json()
    assert refreshed["review_counts"]["verified"] == 1


def test_audio_endpoints_report_missing_variants(client, witness_sample):
    recording = upload_ok(client, witness_sample)

    denoised = client.get(
        f"/api/recordings/{recording['id']}/audio", params={"variant": "denoised"}
    )
    assert denoised.status_code == 404
    assert "No 'denoised' audio" in denoised.json()["detail"]

    missing = client.get("/api/recordings/rec_doesnotexist/audio")
    assert missing.status_code == 404


def test_range_request_beyond_file_returns_416(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    response = client.get(
        f"/api/recordings/{recording['id']}/audio",
        headers={"Range": "bytes=999999999-1000000000"},
    )
    assert response.status_code == 416


def test_health_reports_capabilities(client):
    payload = client.get("/api/health").json()

    assert payload["status"] == "ok"
    assert payload["database"] == "up"
    names = {capability["name"] for capability in payload["capabilities"]}
    assert {"database", "ffmpeg", "transcription", "denoising", "embeddings", "vector_store"} <= names
    assert payload["embedder"] == "hashing-lexical-v2"
    assert payload["vector_store"] == "local-numpy"


def test_health_surfaces_the_pinned_asr_language(client, set_env):
    """The language must be visible: a misdetected transcript looks like a bad
    transcript, not a configuration problem."""
    set_env(TRANSCRIBER="faster-whisper", WHISPER_LANGUAGE="en")
    payload = client.get("/api/health").json()
    transcription = next(
        capability for capability in payload["capabilities"]
        if capability["name"] == "transcription"
    )
    if transcription["available"]:
        assert "language=en" in transcription["detail"]


def test_me_endpoint_exposes_permissions(client):
    payload = client.get("/api/me", headers=REVIEWER_HEADERS).json()
    assert payload["role"] == "reviewer"
    assert "verify" in payload["permissions"]
    assert "upload" not in payload["permissions"]
