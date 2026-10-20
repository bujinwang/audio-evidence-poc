"""FR-01: audio library, upload integrity, and access control."""

from __future__ import annotations

import hashlib
import stat

from conftest import ADMIN_HEADERS, VIEWER_HEADERS, upload, upload_ok

from app.config import get_settings


def test_upload_creates_searchable_recording(client, witness_sample):
    data = upload_ok(client, witness_sample)

    assert data["status"] == "ready"
    assert data["source_type"] == "interview"
    assert data["segment_count"] == 5
    assert data["variants"] == ["original"]
    assert data["latest_job"]["status"] == "succeeded"
    assert data["latest_job"]["progress"] == 1.0
    assert data["duration_s"] and data["duration_s"] > 5
    assert data["review_counts"]["ai_generated"] == 5


def test_original_is_preserved_read_only_and_unmodified(client, witness_sample):
    audio_path, _ = witness_sample
    original_bytes = audio_path.read_bytes()
    expected_sha = hashlib.sha256(original_bytes).hexdigest()

    data = upload_ok(client, witness_sample)

    stored = get_settings().originals_dir / data["id"] / audio_path.name
    assert stored.exists()
    assert stored.read_bytes() == original_bytes, "original audio must never be rewritten"
    assert data["sha256"] == expected_sha

    mode = stat.S_IMODE(stored.stat().st_mode)
    assert mode & 0o222 == 0, "original should be write-protected"


def test_working_copy_is_a_separate_derivative(client, witness_sample):
    data = upload_ok(client, witness_sample)
    settings = get_settings()
    working = settings.working_dir / f"{data['id']}.flac"
    original = settings.originals_dir / data["id"] / witness_sample[0].name

    if not working.exists():
        # FFmpeg absent in this environment: the pipeline must say so, not fail.
        assert data["processing_meta"]["working_copy"]["status"] == "skipped"
        return

    assert working != original
    assert working.stat().st_size > 0
    assert original.read_bytes() == witness_sample[0].read_bytes()


def test_unsupported_extension_is_rejected(client):
    response = client.post(
        "/api/recordings",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"source_type": "other"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 415
    assert "Unsupported audio type" in response.json()["detail"]


def test_empty_file_is_rejected(client):
    response = client.post(
        "/api/recordings",
        files={"file": ("empty.wav", b"", "audio/wav")},
        data={"source_type": "other"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 400


def test_invalid_reference_transcript_is_rejected(client, witness_sample):
    audio_path, _ = witness_sample
    response = client.post(
        "/api/recordings",
        files={
            "file": (audio_path.name, audio_path.read_bytes(), "audio/wav"),
            "transcript_file": ("bad.json", b"{not json", "application/json"),
        },
        data={"source_type": "interview"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]


def test_upload_requires_upload_permission(client, witness_sample):
    response = upload(client, witness_sample, headers=VIEWER_HEADERS)
    assert response.status_code == 403
    assert "may not perform 'upload'" in response.json()["detail"]


def test_duplicate_upload_is_flagged_not_blocked(client, witness_sample):
    first = upload_ok(client, witness_sample)
    second = upload_ok(client, witness_sample)

    assert first["id"] != second["id"]
    assert second["processing_meta"]["duplicate_of"] == first["id"]


def test_unknown_role_is_rejected(client):
    response = client.get("/api/recordings", headers={"X-Actor": "x", "X-Role": "wizard"})
    assert response.status_code == 403
    assert "Unknown role" in response.json()["detail"]


def test_library_listing_and_filters(client, witness_sample, bodycam_sample):
    upload_ok(client, witness_sample, source_type="interview")
    upload_ok(client, bodycam_sample, source_type="bodycam")

    everything = client.get("/api/recordings").json()
    assert len(everything) == 2

    bodycams = client.get("/api/recordings", params={"source_type": "bodycam"}).json()
    assert len(bodycams) == 1
    assert bodycams[0]["source_type"] == "bodycam"

    by_name = client.get("/api/recordings", params={"q": "witness"}).json()
    assert len(by_name) == 1
