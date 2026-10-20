"""Triage, on-demand denoise, and chain-of-custody behaviour.

These cover the three things Ling Huang's reply and the Technology North
service videos changed in the design:

* a reopened case needs twenty minutes out of thousands of hours, and nobody
  will pay to fully index every hour of tape — so intake must be cheap and
  promotion must be per-recording;
* the digitisation service records who did what to each file, and when, at
  every stage for full traceability;
* denoising is demonstrated as a clip A/B'd on the spot, so it has to be
  reachable as an action, not only as a step buried inside ingest.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import ADMIN_HEADERS, upload, upload_ok


def triage_ok(client: TestClient, sample, *, source_type: str = "cassette") -> dict:
    audio_path, _ = sample
    response = client.post(
        "/api/recordings/triage",
        files={"file": (audio_path.name, audio_path.read_bytes(), "audio/wav")},
        data={"source_type": source_type, "recorded_at": "1998-04-02T09:00:00-06:00"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# triage
# --------------------------------------------------------------------------- #

def test_triage_registers_the_file_without_transcribing_it(client, cassette_sample):
    record = triage_ok(client, cassette_sample)

    assert record["status"] == "triaged"
    assert record["segment_count"] == 0
    assert record["has_denoised"] is False
    assert record["has_original"] is True
    # The file is still identified and citable even though nobody paid to index it.
    assert record["sha256"]
    assert record["duration_s"] and record["duration_s"] > 0


def test_triage_records_that_it_did_not_transcribe(client, cassette_sample):
    record = triage_ok(client, cassette_sample)

    intake = record["processing_meta"]["intake"]
    assert intake["mode"] == "triage"
    assert intake["transcribed"] is False
    assert "denoise" not in record["processing_meta"]


def test_triaged_audio_is_still_playable_and_seekable(client, cassette_sample):
    record = triage_ok(client, cassette_sample)

    response = client.get(
        f"/api/recordings/{record['id']}/audio",
        headers={**ADMIN_HEADERS, "Range": "bytes=0-1023"},
    )
    assert response.status_code == 206
    assert "Content-Range" in response.headers


def test_a_triaged_recording_contributes_nothing_to_search(client, cassette_sample, set_env):
    set_env(EMBEDDER="hashing")
    triage_ok(client, cassette_sample)

    response = client.get("/api/search?q=anything&mode=keyword", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_a_triaged_recording_can_be_promoted_when_a_case_needs_it(client, cassette_sample):
    """Promotion runs the full pipeline; it does not have to yield segments to count.

    This environment has no ASR engine, so the assertion is that the recording
    reaches `ready` and that full processing is recorded — not that text appears.
    """
    record = triage_ok(client, cassette_sample)
    assert record["segment_count"] == 0

    promoted = client.post(
        f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS
    )
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert body["status"] == "ready"
    assert "transcription" in body["processing_meta"]


def test_triage_still_refuses_a_non_audio_file(client):
    response = client.post(
        "/api/recordings/triage",
        files={"file": ("notes.txt", b"not audio", "text/plain")},
        data={"source_type": "other"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 415


# --------------------------------------------------------------------------- #
# on-demand denoise
# --------------------------------------------------------------------------- #

def _stub_denoiser_module():
    """A denoiser that copies the source, so the derivative path is exercised."""
    import shutil

    from app.pipeline.denoise import DenoiseResult

    class StubDenoiser:
        name = "stub"

        def available(self):
            return True, "stub denoiser for tests"

        def denoise(self, src, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return DenoiseResult(path=dst, engine="stub", settings={"api": "test-stub"})

    return StubDenoiser()


def test_denoise_can_be_run_after_ingest(client, witness_sample, monkeypatch):
    """A recording ingested where the denoiser was absent can still be denoised."""
    record = upload_ok(client, witness_sample)
    assert record["has_denoised"] is False, "fixture expectation: no denoise on ingest"

    from app.pipeline import jobs as jobs_module

    # `jobs` binds get_denoiser at import time, so patch it there.
    monkeypatch.setattr(jobs_module, "get_denoiser", lambda *a, **k: _stub_denoiser_module())

    response = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_denoised"] is True
    assert body["processing_meta"]["denoise"]["status"] == "ok"

    audio = client.get(
        f"/api/recordings/{record['id']}/audio?variant=denoised", headers=ADMIN_HEADERS
    )
    assert audio.status_code == 200
    assert len(audio.content) > 0


def test_denoising_never_touches_the_original(client, witness_sample, monkeypatch):
    """A derivative is new data; the exhibit must be byte-identical afterwards."""
    import hashlib

    audio_path, _ = witness_sample
    source_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    record = upload_ok(client, witness_sample)
    assert record["sha256"] == source_hash

    from app.pipeline import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "get_denoiser", lambda *a, **k: _stub_denoiser_module())
    assert client.post(
        f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS
    ).status_code == 200

    original = client.get(f"/api/recordings/{record['id']}/audio", headers=ADMIN_HEADERS)
    assert hashlib.sha256(original.content).hexdigest() == source_hash
    # And the recording still reports the original digest, not the derivative's.
    assert client.get(
        f"/api/recordings/{record['id']}", headers=ADMIN_HEADERS
    ).json()["sha256"] == source_hash


def test_denoise_is_recorded_in_the_custody_chain(client, witness_sample, monkeypatch):
    record = upload_ok(client, witness_sample)

    from app.pipeline import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "get_denoiser", lambda *a, **k: _stub_denoiser_module())
    client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()
    stages = [step["stage"] for step in provenance["steps"]]
    assert "recording.denoised" in stages


def test_denoising_with_no_engine_configured_reports_it(client, witness_sample):
    """DENOISER=none in the test environment: the action must fail loudly."""
    record = upload_ok(client, witness_sample)
    response = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()


def test_denoise_requires_a_permission_that_a_viewer_lacks(client, witness_sample):
    record = upload_ok(client, witness_sample)
    response = client.post(
        f"/api/recordings/{record['id']}/denoise",
        headers={"X-Actor": "demo.viewer", "X-Role": "viewer"},
    )
    assert response.status_code == 403


def test_denoise_refuses_to_race_a_job_that_is_still_running(client, witness_sample):
    """A queued or running job owns the working copy; denoising must not collide."""
    record = upload_ok(client, witness_sample)

    # Park a job in `running` directly: in-process tests never leave one behind.
    from app.db import session_scope
    from app.models import Job

    with session_scope() as session:
        session.add(
            Job(
                recording_id=record["id"],
                kind="process",
                status="running",
                stage="transcribe",
                progress=0.5,
            )
        )

    response = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)
    assert response.status_code == 409
    assert "queued or running" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# chain of custody
# --------------------------------------------------------------------------- #

def test_custody_chain_names_receipt_and_completion(client, witness_sample):
    record = upload_ok(client, witness_sample)

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()

    assert provenance["sha256"] == record["sha256"]
    stages = [step["stage"] for step in provenance["steps"]]
    assert "recording.uploaded" in stages
    assert any(s.startswith("processing.completed") for s in stages)


def test_custody_chain_is_ordered_oldest_first(client, witness_sample):
    record = upload_ok(client, witness_sample)
    client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()

    stamps = [step["at"] for step in provenance["steps"] if step["at"]]
    assert stamps == sorted(stamps)


def test_custody_chain_attributes_who_did_the_work(client, witness_sample):
    """The intake actor is attributed, and automation is labelled as system."""
    record = upload(client, witness_sample).json()
    assert "id" in record, record

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()

    actors = {step["actor"] for step in provenance["steps"] if step["actor"]}
    assert actors, "every custody step should name an actor"
    assert any("admin" in (a or "") or a == "system" for a in actors)


def test_custody_chain_hides_playback_noise(client, witness_sample):
    """Playback is access logging, not handling history — it must not bury the chain."""
    record = upload_ok(client, witness_sample)
    for _ in range(5):
        client.get(f"/api/recordings/{record['id']}/audio", headers=ADMIN_HEADERS)

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()

    stages = [step["stage"] for step in provenance["steps"]]
    assert "audio.played" not in stages
    # The chain stays short enough to read.
    assert len(stages) <= 8


def test_a_triaged_recording_reads_as_triage_in_the_chain(client, cassette_sample):
    record = triage_ok(client, cassette_sample)

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()

    assert provenance["intake_mode"] == "triage"
    stages = [step["stage"] for step in provenance["steps"]]
    assert "recording.triaged" in stages


def test_promotion_after_triage_shows_both_halves_of_the_history(client, cassette_sample):
    record = triage_ok(client, cassette_sample)
    client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)

    provenance = client.get(
        f"/api/recordings/{record['id']}/provenance", headers=ADMIN_HEADERS
    ).json()

    assert provenance["intake_mode"] == "triage_promoted"
    stages = [step["stage"] for step in provenance["steps"]]
    assert "recording.triaged" in stages
    assert any(s.startswith("processing.completed") for s in stages)
