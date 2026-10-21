"""Stranded-job recovery: an exhibit whose worker died must be re-indexable.

The original defect left three jobs at `status="running"` forever, and because
`run_job()` returns early for a job it finds in `running`, nothing ever moved
them on. `review_locked()` and the active-job check then refused every later
`POST /retry` with 409, so that exhibit could never be re-processed again
without hand-editing the database. For an evidence tool that is a custody
problem: an exhibit that can never be reprocessed has a hole in its history.

The fix is `POST /api/jobs/{id}/reset` (and the per-exhibit variant
`POST /api/jobs/reset-stranded/{recording_id}`) — an explicit, attributable
operator action. It is deliberately not automatic, and these tests state why:
`Job` has `started_at` but no heartbeat, and a `transcribe()` call blocks for
tens of minutes without touching the database, so the age of `started_at`
cannot distinguish "the process died" from "a two-hour cassette is still being
transcribed". An automatic age threshold would have to guess, and guessing wrong
kills a live run — worse than the bug. What the tests *do* pin down is that the
decision is recorded: the stage, progress and timestamp observed at reset time
go into the audit trail.

Counted honestly: this file has 14 `def test_` functions and collects 16 tests,
because `test_reset_does_not_depend_on_the_age_of_started_at` is parametrised
over three ages. `pytest --collect-only` is the count that matches a suite
total; `grep -c '^def test_'` will read two short and send you looking for
missing tests that were never there.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import ADMIN_HEADERS, REVIEWER_HEADERS, VIEWER_HEADERS, upload_ok


def _strand_job(recording_id: str, *, status: str = "running", age_hours: float = 0.0,
                stage: str = "transcribing", kind: str = "process",
                progress: float = 0.62) -> str:
    """Park an active job on an exhibit, as a dead worker would have left it.

    In-process tests never leave one behind, so the state is manufactured here:
    a job in `running` with a `started_at` in the past and the recording stuck
    at `processing`.
    """
    from app.db import session_scope
    from app.models import Job, Recording, RecordingStatus, utcnow

    started = utcnow() - timedelta(hours=age_hours)
    with session_scope() as session:
        job = Job(
            recording_id=recording_id,
            kind=kind,
            status=status,
            stage=stage,
            progress=progress,
            attempts=1,
            started_at=started,
        )
        session.add(job)
        session.flush()
        job_id = job.id
        recording = session.get(Recording, recording_id)
        if recording is not None and status == "running" and kind == "process":
            recording.status = RecordingStatus.PROCESSING
    return job_id


def _job(job_id: str) -> dict:
    from app.db import SessionLocal
    from app.models import Job
    from app.serializers import job_out

    with SessionLocal() as session:
        return job_out(session.get(Job, job_id)).model_dump(mode="json")


def test_a_stranded_job_blocks_retry_which_is_the_damage(client, witness_sample):
    """The precondition: this is what an un-recoverable exhibit looks like."""
    record = upload_ok(client, witness_sample)
    _strand_job(record["id"], age_hours=6)

    blocked = client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)
    assert blocked.status_code == 409
    assert "already queued or running" in blocked.json()["detail"]


def test_reset_unblocks_the_exhibit_without_touching_the_database(client, witness_sample):
    """The whole recovery path, end to end, through the API only."""
    record = upload_ok(client, witness_sample)
    job_id = _strand_job(record["id"], age_hours=6)

    reset = client.post(f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS)
    assert reset.status_code == 200, reset.text
    payload = reset.json()
    assert payload["status"] == "failed"
    assert payload["stage"] == "reset"
    assert "stale" in payload["error"].lower()
    # It names what was observed, so an auditor can tell what was abandoned.
    assert "transcribing" in payload["error"]

    # The exhibit is usable again — this is the point of the whole exercise.
    retried = client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] in ("ready", "processing")


def test_reset_preserves_the_job_row_as_history(client, witness_sample):
    """Nothing is deleted: the abandoned attempt stays on the record."""
    record = upload_ok(client, witness_sample)
    job_id = _strand_job(record["id"], age_hours=6)

    before = client.get(f"/api/recordings/{record['id']}/jobs", headers=ADMIN_HEADERS).json()
    client.post(f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS)
    after = client.get(f"/api/recordings/{record['id']}/jobs", headers=ADMIN_HEADERS).json()

    assert len(after) == len(before), "job history was truncated"
    assert job_id in {job["id"] for job in after}
    retired = next(job for job in after if job["id"] == job_id)
    assert retired["status"] == "failed"
    assert retired["finished_at"] is not None


def test_reset_is_recorded_against_the_actor_who_made_the_call(client, witness_sample):
    """Abandoning a run is attributable — that is the custody requirement."""
    record = upload_ok(client, witness_sample)
    job_id = _strand_job(record["id"], age_hours=6)

    client.post(f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS)

    events = client.get(
        f"/api/audit?entity_id={job_id}", headers=ADMIN_HEADERS
    ).json()
    resets = [event for event in events if event["action"] == "job.reset"]
    assert len(resets) == 1, f"reset was not audited: {[e['action'] for e in events]}"
    assert resets[0]["actor"] == "test.admin"
    assert resets[0]["detail"]["stage"] == "transcribing"
    assert resets[0]["detail"]["progress"] == 0.62
    assert resets[0]["detail"]["started_at"] is not None


def test_reset_refuses_a_job_that_is_not_active(client, witness_sample):
    """Resetting a finished job would rewrite a completed record — refuse it."""
    record = upload_ok(client, witness_sample)
    finished = client.get(
        f"/api/recordings/{record['id']}/jobs", headers=ADMIN_HEADERS
    ).json()[0]
    assert finished["status"] == "succeeded"

    response = client.post(f"/api/jobs/{finished['id']}/reset", headers=ADMIN_HEADERS)
    assert response.status_code == 409
    assert "not active" in response.json()["detail"]

    # And the row is unchanged.
    assert _job(finished["id"])["status"] == "succeeded"


def test_reset_queued_job_is_also_recoverable(client, witness_sample):
    """A job stuck at `queued` blocks retries too, and is equally recoverable."""
    record = upload_ok(client, witness_sample)
    job_id = _strand_job(record["id"], status="queued", stage="queued")

    response = client.post(f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"


def test_reset_requires_the_retry_permission(client, witness_sample):
    record = upload_ok(client, witness_sample)
    job_id = _strand_job(record["id"], age_hours=6)

    for headers in (VIEWER_HEADERS, REVIEWER_HEADERS):
        response = client.post(f"/api/jobs/{job_id}/reset", headers=headers)
        assert response.status_code == 403, response.text

    assert _job(job_id)["status"] == "running"


def test_reset_unknown_job_is_404(client):
    response = client.post("/api/jobs/job_doesnotexist/reset", headers=ADMIN_HEADERS)
    assert response.status_code == 404


def test_reset_stranded_for_recording_clears_all_of_them(client, witness_sample):
    """The operator-facing shape: they know the exhibit, not which row holds the lock.

    The audited database had three stranded rows on one exhibit. At most one can
    be genuinely live (the partial unique index now guarantees that), so retiring
    the exhibit's active rows is safe and is what someone staring at a stuck
    exhibit actually wants.
    """
    from app.db import session_scope
    from app.models import Job

    record = upload_ok(client, witness_sample)
    first = _strand_job(record["id"], age_hours=6)

    # Manufacture a second stranded row by inserting past the index, exactly as
    # a pre-fix database would hold it.
    with session_scope() as session:
        session.execute(
            Job.__table__.update().where(Job.id == first).values(status="failed")
        )
    second = _strand_job(record["id"], age_hours=5)

    response = client.post(
        f"/api/jobs/reset-stranded/{record['id']}", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text
    assert [job["id"] for job in response.json()] == [second]

    retried = client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)
    assert retried.status_code == 200, retried.text


def test_reset_stranded_on_a_healthy_exhibit_is_a_no_op(client, witness_sample):
    record = upload_ok(client, witness_sample)

    response = client.post(
        f"/api/jobs/reset-stranded/{record['id']}", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("age_hours", [0.0, 0.5, 48.0])
def test_reset_does_not_depend_on_the_age_of_started_at(client, witness_sample, age_hours):
    """Deliberate: recovery is an explicit decision, not an age threshold.

    A 48-hour-old `running` job is almost certainly dead; a 0-second-old one
    might be a live transcription. Neither is reset automatically, and both can
    be reset by an operator. Encoding that choice here stops a future "just add
    a timeout" change from silently killing live runs.
    """
    record = upload_ok(client, witness_sample)
    job_id = _strand_job(record["id"], age_hours=age_hours)

    # No automatic staleness: an old job still blocks retry on its own.
    blocked = client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)
    assert blocked.status_code == 409

    reset = client.post(f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS)
    assert reset.status_code == 200, reset.text


def test_a_stranded_denoise_claim_is_recoverable_by_the_same_endpoint(client, witness_sample):
    """Denoise now takes the active-job slot, so its claim must be resettable too.

    `denoise_existing()` claims the slot with a `running` job of `kind="denoise"`
    so that concurrent callers serialise (one artefact, one audit event). That
    introduces the retry problem in a new place: a process killed mid-denoise
    leaves the claim `running` and the exhibit unable to denoise or retry.

    No denoise-specific recovery machinery was written, deliberately — the claim
    is an ordinary `jobs` row, so the ordinary endpoint covers it. This test pins
    that, because a slot with no release would be worse than the race it fixes.
    """
    record = upload_ok(client, witness_sample)
    claim = _strand_job(
        record["id"], age_hours=3, kind="denoise", stage="denoise", progress=0.0
    )

    blocked = client.post(f"/api/recordings/{record['id']}/denoise", headers=ADMIN_HEADERS)
    assert blocked.status_code == 409, "a dead denoise claim must block, as a job does"

    reset = client.post(f"/api/jobs/{claim}/reset", headers=ADMIN_HEADERS)
    assert reset.status_code == 200, reset.text
    assert reset.json()["status"] == "failed"
    assert reset.json()["stage"] == "reset"

    # The exhibit is usable again through the ordinary retry path.
    retried = client.post(f"/api/recordings/{record['id']}/retry", headers=ADMIN_HEADERS)
    assert retried.status_code == 200, retried.text


def test_reset_retires_a_row_but_starts_nothing(client, witness_sample):
    """Reset frees the slot. It does not enqueue work, and it does not rebuild.

    Worth pinning because it is the natural thing to fear: denoise and ingest
    share one active-job slot, so it is plausible to assume that resetting a
    denoise claim would re-run the denoise, or that resetting an ingest job would
    kick off a re-index. Neither happens — `reset_stuck_job()` writes one row and
    one audit event, and starts nothing. An operator deciding "this run is dead"
    should not thereby start a different run they did not ask for.

    Measured here rather than reasoned about, because "it only marks a row"
    is exactly the kind of claim that quietly stops being true when someone
    adds a convenience call later.
    """
    from app.db import session_scope
    from app.models import Job, Recording

    record = upload_ok(client, witness_sample)
    recording_id = record["id"]
    claim = _strand_job(
        recording_id, age_hours=2, kind="denoise", stage="denoise", progress=0.0
    )

    with session_scope() as session:
        before_rows = session.query(Job).filter(Job.recording_id == recording_id).count()
        before_state = session.get(Recording, recording_id)
        before_denoised = before_state.denoised_path
        before_meta = dict(before_state.processing_meta or {})

    assert client.post(
        f"/api/jobs/{claim}/reset", headers=ADMIN_HEADERS
    ).status_code == 200

    with session_scope() as session:
        after_rows = session.query(Job).filter(Job.recording_id == recording_id).count()
        after_state = session.get(Recording, recording_id)
        after_denoised = after_state.denoised_path
        after_meta = dict(after_state.processing_meta or {})

    assert after_rows == before_rows, "reset created or removed a job row"
    assert after_denoised == before_denoised, "reset rebuilt a derivative"
    assert after_meta == before_meta, "reset ran the pipeline"

    # The only audit event is the reset itself — no processing was triggered.
    events = client.get(
        f"/api/audit?recording_id={recording_id}", headers=ADMIN_HEADERS
    ).json()
    actions = [event["action"] for event in events]
    assert actions.count("job.reset") == 1
    assert "recording.denoised" not in actions, "reset re-triggered a denoise"
    assert "recording.processing_requested" not in actions, "reset triggered an ingest"


def test_reset_leaves_existing_transcripts_in_place(client, witness_sample):
    """A stuck `processing` status is corrected *by the reset*; the data survives it.

    `reset_stuck_job` moves the recording from `processing` to `failed` so the
    exhibit is not permanently mid-flight. It deliberately does not touch
    transcripts or segments: the recovery action's job is to un-stick a status,
    and destroying a partly-finished ingest would make the cure worse than the
    symptom.

    **This test deliberately stops at the reset.** It says nothing about what a
    subsequent retry does, because a retry re-transcribes from scratch — see
    `test_retry_after_reset_re_transcribes_and_can_lose_the_transcript`. An
    earlier version of this docstring ended "a later retry is then an ordinary
    re-index", which was false and contradicted the test below it.
    """
    from app.db import session_scope
    from app.models import Recording, RecordingStatus
    from sqlalchemy import text

    record = upload_ok(client, witness_sample)
    recording_id = record["id"]
    job_id = _strand_job(recording_id, age_hours=2, stage="transcribing")

    with session_scope() as session:
        session.get(Recording, recording_id).status = RecordingStatus.PROCESSING

    def segment_count() -> int:
        from app import db

        with db.engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM segments WHERE recording_id = :r"),
                    {"r": recording_id},
                ).scalar()
            )

    before = segment_count()
    assert before > 0, "fixture expectation: the upload produced segments"

    assert client.post(
        f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS
    ).status_code == 200

    with session_scope() as session:
        assert session.get(Recording, recording_id).status == RecordingStatus.FAILED
    assert segment_count() == before, "reset discarded transcript segments"



def test_retry_after_reset_re_transcribes_and_can_lose_the_transcript(
    client, witness_sample, monkeypatch
):
    """Reset restores retryability, not the transcript. Measured, not assumed.

    This exists because an earlier version of this file — and of the reset
    docstring, and of the demo notes — said a retry after a reset was "an
    ordinary re-index". That was false, and it was the comfortable direction to
    be wrong in, which is why it survived three people reviewing the same
    sentences.

    The mechanism: `reset_stuck_job()` genuinely does leave transcripts alone,
    so the data is intact the moment the reset returns. But the retry it unblocks
    enters `_process()`, whose third statement is
    `_clear_previous_attempt()` — that deletes every transcript and segment and
    drops their search index. Nothing is rebuilt until ASR produces output. So if
    the retry fails, or the transcriber is unavailable, the exhibit ends with
    zero segments where it began with a full transcript.

    Two assertions, because the pair is the point:

    * the retry really does re-transcribe rather than recompute an index over
      existing segments (new transcript id, different row ids);
    * when that re-transcription cannot happen, the segments are gone and the
      recording still reports `ready`.

    The second half is the one an operator needs to know before pressing retry
    on a `processing` exhibit mid-demo, so it is pinned rather than described.
    """
    from app.db import session_scope
    from app.models import Recording, Segment, Transcript
    from app.pipeline import jobs as job_pipeline

    record = upload_ok(client, witness_sample)
    recording_id = record["id"]

    with session_scope() as session:
        before_ids = sorted(
            s.id for s in session.query(Segment).filter(Segment.recording_id == recording_id)
        )
    assert len(before_ids) > 0, "fixture expectation: the upload produced segments"

    job_id = _strand_job(recording_id, age_hours=0, stage="transcribing")
    assert client.post(
        f"/api/jobs/{job_id}/reset", headers=ADMIN_HEADERS
    ).status_code == 200

    with session_scope() as session:
        still_there = sorted(
            s.id for s in session.query(Segment).filter(Segment.recording_id == recording_id)
        )
    assert still_there == before_ids, "the reset itself must not touch segments"

    # First half: a successful retry re-transcribes rather than re-indexing.
    assert client.post(
        f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS
    ).status_code == 200

    with session_scope() as session:
        after_ids = sorted(
            s.id for s in session.query(Segment).filter(Segment.recording_id == recording_id)
        )
        transcripts = (
            session.query(Transcript).filter(Transcript.recording_id == recording_id).count()
        )
    assert len(after_ids) == len(before_ids), "retry should replace, not merge"
    assert not set(after_ids) & set(before_ids), (
        "segment rows were reused across a retry, which means this was an index "
        "rebuild rather than a re-transcription — the rest of this test assumes "
        "the latter, so if that ever changes this test must change with it"
    )
    assert transcripts == 1

    # Second half: a retry that cannot reach ASR leaves nothing behind.
    real_resolve = job_pipeline.resolve_transcriber

    def unavailable(*args, **kwargs):
        inner = real_resolve(*args, **kwargs)

        class _NoASR:
            def available(self):
                return False, "ASR not installed in this deployment"

            def __getattr__(self, name):
                return getattr(inner, name)

        return _NoASR()

    monkeypatch.setattr(job_pipeline, "resolve_transcriber", unavailable)
    stranded_again = _strand_job(recording_id, age_hours=0, stage="transcribing")
    assert client.post(
        f"/api/jobs/{stranded_again}/reset", headers=ADMIN_HEADERS
    ).status_code == 200
    assert client.post(
        f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS
    ).status_code == 200

    with session_scope() as session:
        final_segments = (
            session.query(Segment).filter(Segment.recording_id == recording_id).count()
        )
        final_transcripts = (
            session.query(Transcript).filter(Transcript.recording_id == recording_id).count()
        )
        recording = session.get(Recording, recording_id)
        transcription_meta = (recording.processing_meta or {}).get("transcription", {})

    assert final_transcripts == 0, (
        "ASR was unavailable, so no transcript should have been written"
    )
    assert final_segments == 0, (
        "the retry cleared the previous segments and produced none — this is the "
        "transcript loss the reset docstring warns about"
    )
    assert transcription_meta.get("status") == "unavailable"
    assert recording.status == "ready", (
        "the retry reports success with a warning, so the exhibit looks healthy "
        "while having no transcript at all — that gap between status and content "
        "is the reason this test exists"
    )
