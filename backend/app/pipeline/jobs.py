"""Asynchronous processing orchestration.

One job == one full ingest of one recording. Progress is committed after every
stage so the library screen can poll a live percentage, and every stage records
either what it did or why it could not (PRD FR-02, acceptance criterion 3).

Retry policy: re-processing is refused if any segment has been human-edited or
verified, because wiping transcripts would destroy review history. The API
surfaces that as a 409 with the reason.

Concurrency: "at most one active (queued or running) job per recording" is a
database invariant (`uq_job_active_recording`, a partial unique index), not a
check performed here. Every path that starts work — `/retry`, upload ingest, and
on-demand `/denoise` — takes that one slot before doing anything, so two callers
cannot both believe they have the exhibit to themselves. A slot held by a dead
process is released by `reset_stuck_job()` (exposed as an explicit operator
endpoint), never automatically: without a heartbeat, age cannot distinguish a
dead run from a slow one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit, indexing, storage
from ..config import get_settings
from ..db import STALE_ERROR, SessionLocal
from ..errors import ActiveJobConflict, DependencyUnavailable, ProcessingError
from ..models import (
    Job,
    JobStatus,
    Recording,
    RecordingStatus,
    ReviewStatus,
    Segment,
    SegmentEmbedding,
    Transcript,
    TranscriptVariant,
    new_id,
    utcnow,
)
from . import audio as audio_tools
from .denoise import get_denoiser
from .embed import get_embedder
from .transcribe import SidecarTranscriber, TranscriptionResult, resolve_transcriber
from .vectorstore import VectorItem, get_vector_store

logger = logging.getLogger(__name__)

EMBED_BATCH = 32


# --------------------------------------------------------------------------- #
# queue helpers
# --------------------------------------------------------------------------- #

def _is_active_job_conflict(exc: IntegrityError) -> bool:
    """Did this IntegrityError come from `uq_job_active_recording`?

    SQLite reports a partial-index violation as `UNIQUE constraint failed:
    jobs.recording_id` — it does not name the index — while PostgreSQL names the
    constraint. Both are matched here. Any other IntegrityError (a missing
    foreign key, some future constraint) must keep propagating: swallowing it as
    "409" would hide a real defect behind a plausible status code.
    """
    detail = f"{exc.orig or exc}".lower()
    return "uq_job_active_recording" in detail or "jobs.recording_id" in detail


def enqueue(
    session: Session,
    recording: Recording,
    actor: str,
    kind: str = "process",
) -> Job:
    """Insert a queued job, atomically, or raise `ActiveJobConflict`.

    The "at most one active job per recording" rule is enforced by the partial
    unique index `uq_job_active_recording`, so this does not check-then-act: the
    insert either wins or the database rejects it. Two callers that both decided
    "no job is running" therefore cannot both create one — the second is told to
    wait, exactly as if it had arrived a moment later.

    **This never supersedes an active job.** There is no "if the running job
    looks abandoned, take its place" branch and there must not be one: `Job` has
    no heartbeat, and the long stages block for tens of minutes without touching
    the database, so age cannot distinguish a dead job from a slow one. Anyone
    reading this expecting an automatic takeover is looking for
    `reset_stuck_job()` (exposed as `POST /api/jobs/{id}/reset`), which is an
    explicit, attributed operator decision.

    The caller owns the commit: the job insert and the audit event describing it
    must land together or not at all.
    """
    job = Job(recording_id=recording.id, kind=kind, status=JobStatus.QUEUED, actor=actor)
    session.add(job)
    recording.status = RecordingStatus.UPLOADED
    recording.error = None
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if _is_active_job_conflict(exc):
            raise ActiveJobConflict(recording.id) from exc
        raise
    return job


def submit(session: Session, recording: Recording, actor: str) -> Job:
    """Enqueue, and process inline when configured (single-process demo mode)."""
    job = enqueue(session, recording, actor)
    session.commit()
    _run_inline(session, job)
    return job


def request_processing(session: Session, recording: Recording, actor) -> Job:
    """`POST /retry` body: one atomic "start a fresh run" for one recording.

    This exists as its own function because the ordering matters and is easy to
    get wrong in a route handler:

    1. The caller has already run the review-lock and active-job checks. Those
       reads opened an implicit transaction. On SQLite a read snapshot cannot be
       upgraded to a write once another connection has committed
       (`SQLITE_BUSY_SNAPSHOT`), which would surface as a spurious 500 under
       exactly the concurrent load this fix is about — so the snapshot is ended
       and the insert starts a fresh transaction.
    2. The insert is guard-railed by the unique index, not by a prior SELECT.
    3. The audit event and the job row commit together, so the custody chain
       never records a request that did not happen, nor a job with no record of
       who asked for it.
    """
    recording_id = recording.id
    previous_status = recording.status
    previous_error = recording.error

    session.rollback()

    fresh = session.get(Recording, recording_id)
    if fresh is None:
        raise ProcessingError("recording vanished while requesting re-processing")

    job = enqueue(session, fresh, actor.name if hasattr(actor, "name") else str(actor))
    audit.record(
        session, actor, "recording.processing_requested",
        entity_type="recording", entity_id=recording_id, recording_id=recording_id,
        detail={
            "job_id": job.id,
            "previous_status": previous_status,
            "previous_error": previous_error,
        },
    )
    session.commit()
    _run_inline(session, job)
    return job


def reset_stuck_job(session: Session, job: Job, actor) -> Job:
    """Retire a queued/running job that is not actually progressing.

    The recovery path for a job whose process died mid-run — which is not
    hypothetical: the concurrent-retry defect left three jobs in `running` on
    one exhibit, and because `review_locked()`/the active-job check refuse a new
    run while one is active, that exhibit could never be reprocessed again.
    "Reprocessed", not "repaired": this function preserves the existing
    transcripts and segments, but the re-run it enables starts by clearing them
    (`_clear_previous_attempt()`), so recovery means the exhibit is workable
    again rather than that the earlier transcript survives.

    Why an explicit operator action rather than an automatic staleness rule:
    without a heartbeat written during the long blocking stages (a single
    `transcribe()` call on a two-hour cassette can run for tens of minutes
    without touching the database), `started_at` age cannot distinguish "dead"
    from "slow". A threshold would therefore have to guess, and guessing wrong
    destroys a live run — discarding derivatives an exhibit is mid-way through
    producing, which is worse than the bug. This is recorded against the actor
    who made the call, with the stage and progress that were observed at the
    time, so the decision to abandon a run is attributable. For an evidence
    tool that is the correct trade: never silently, always on the record.
    """
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise ActiveJobConflict(
            job.recording_id,
            f"Job {job.id} is not active (status '{job.status}'); nothing to reset.",
        )

    observed = {
        "stage": job.stage,
        "progress": job.progress,
        "attempts": job.attempts,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
    message = STALE_ERROR.format(
        stage=job.stage or "unknown",
        started_at=observed["started_at"] or "never",
    )

    job.status = JobStatus.FAILED
    job.stage = "reset"
    job.error = message
    job.finished_at = utcnow()

    recording = session.get(Recording, job.recording_id)
    if recording is not None and recording.status == RecordingStatus.PROCESSING:
        # It is only honest to un-stick the recording if nothing else is
        # working on it; the partial unique index guarantees there is not.
        recording.status = RecordingStatus.FAILED
        recording.error = message

    audit.record(
        session, actor, "job.reset",
        entity_type="job", entity_id=job.id, recording_id=job.recording_id,
        detail={"reason": "manual reset of a stranded job", **observed},
    )
    session.commit()
    return job


def _run_inline(session: Session, job: Job) -> None:
    """Run the job in-process when configured, then refresh the session."""
    if get_settings().process_inline:
        run_job(job.id)
        session.expire_all()


def run_pending_jobs(limit: int = 5) -> int:
    """Worker entry point: claim queued jobs and run them."""
    processed = 0
    with SessionLocal() as session:
        jobs = list(
            session.execute(
                select(Job)
                .where(Job.status == JobStatus.QUEUED)
                .order_by(Job.created_at)
                .limit(limit)
            ).scalars()
        )
        job_ids = [j.id for j in jobs]
        for job_id in job_ids:
            run_job(job_id)
            processed += 1
    return processed


def review_locked(recording_id: str, session: Session) -> str | None:
    """Return a reason string when re-processing would destroy human review."""
    touched = session.execute(
        select(Segment)
        .where(Segment.recording_id == recording_id)
        .where((Segment.version > 1) | (Segment.review_status != ReviewStatus.AI_GENERATED))
        .limit(1)
    ).scalar_one_or_none()
    if touched is not None:
        return (
            "This recording has reviewed transcript segments. Re-processing would "
            "discard corrections and verification history; open a new recording "
            "instead, or clear review state explicitly."
        )
    return None


# --------------------------------------------------------------------------- #
# triage and targeted promotion
# --------------------------------------------------------------------------- #

def triage(session: Session, recording: Recording, actor: str) -> Recording:
    """Register a recording cheaply: hash, probe, and stop.

    No working copy, no denoise, no ASR, no embeddings. This is the cheap end of
    the cost curve Ling described — a cassette can be citable, playable and
    chain-of-custody-tracked without paying to transcribe a decade of tape.
    Promotion to full processing stays an explicit, per-recording decision.
    """
    meta: dict = dict(recording.processing_meta or {})
    original = Path(recording.original_path)

    probe_status = "unavailable"
    if original.exists():
        try:
            probe = audio_tools.probe(original)
            recording.duration_s = probe.duration_s
            recording.sample_rate = probe.sample_rate
            recording.channels = probe.channels
            meta["probe"] = {
                "status": "ok",
                "duration_s": probe.duration_s,
                "sample_rate": probe.sample_rate,
                "channels": probe.channels,
                "codec": probe.codec,
                "container": probe.container,
            }
            probe_status = "ok"
        except (DependencyUnavailable, ProcessingError) as exc:
            meta["probe"] = {"status": "failed", "reason": str(exc)}
    else:
        meta["probe"] = {"status": "failed", "reason": "original file is missing"}

    meta["intake"] = {
        "mode": "triage",
        "probe": probe_status,
        "transcribed": False,
        "note": (
            "Registered, hashed and probed only. No ASR, no embeddings — promote "
            "this recording when a case needs it searchable."
        ),
    }
    recording.processing_meta = meta
    recording.status = RecordingStatus.TRIAGED
    recording.error = None

    audit.record(
        session, actor, "recording.triaged",
        entity_type="recording", entity_id=recording.id, recording_id=recording.id,
        detail={"probe": probe_status, "duration_s": recording.duration_s},
    )
    session.commit()
    return recording


def denoise_existing(session: Session, recording: Recording, actor) -> Recording:
    """Produce (or refresh) the denoised derivative for an already-ingested file.

    The full pipeline only denoises while ingesting, so a recording triaged
    without denoise — or ingested on a machine where `deepFilter` was absent —
    would never get a derivative. Ling's demo was a single clip A/B'd on the
    spot; this is what makes that possible after the fact.

    **Concurrency.** Two callers used to both get past the route's
    check-then-act and both denoise. Two separate harms followed, and they need
    two separate fixes:

    1. *The artefact.* `storage.denoised_path()` returns one fixed file per
       recording and the denoiser's level-matching step stages to a second fixed
       name, so concurrent runs overwrote each other mid-write; ffmpeg then read
       a truncated input and failed with an EMPTY stderr, surfacing as
       `422 "level matching failed: "` — an error blaming audio levels for a
       race. `_denoise_atomically()` closes that by publishing with one
       `os.replace()` from a private scratch file.
    2. *The custody record.* Even with the write made atomic, all four callers
       completed and all four appended `recording.denoised` to the audit trail
       for a single artefact. In an evidence tool that is the more serious of
       the two: the chain of custody claimed the derivative was rebuilt four
       times when it was produced once, and `/provenance` renders repeats as
       "denoised ×4". A reviewer reconstructing how many times a derivative was
       rebuilt, and by whom, got a false answer.

    So this function now *claims the recording's single active-job slot* before
    doing any work, through the same partial unique index that guards `/retry`.
    One caller wins and produces the derivative and one audit event; the rest
    are told 409, exactly as they would be if they had arrived a moment later.
    Being serialised also means the working copy is no longer built by several
    processes at once.

    The slot is a `jobs` row with `kind="denoise"`, so it is inspectable in the
    ordinary way and — if this process dies mid-denoise — recoverable through
    `POST /api/jobs/{id}/reset` without any denoise-specific machinery. It is
    inserted as `running`, never `queued`, because nobody enqueues it: the
    request that takes the slot is the request that does the work, and a
    `queued` row would be picked up by `run_pending_jobs()` as a full ingest.

    A caller that loses the claim gets `ActiveJobConflict`, which the route maps
    to 409. The derivative is therefore produced once per accepted call rather
    than once per request, and re-denoising after a settings change still works
    — sequentially, one attributable event at a time.
    """
    settings = get_settings()
    recording_id = recording.id

    # Capability probe first: this is not contention, nothing is claimed, and
    # the 503 it becomes tells the operator what to install rather than making
    # them retry. It touches no shared path.
    denoiser = get_denoiser(settings.denoiser)
    ok, reason = denoiser.available()
    if not ok:
        meta: dict = dict(recording.processing_meta or {})
        meta["denoise"] = {"status": "skipped", "reason": reason}
        recording.processing_meta = meta
        session.commit()
        raise DependencyUnavailable("denoiser", reason)

    # The caller's reads opened an implicit transaction. On SQLite a read
    # snapshot cannot be upgraded to a write once another connection has
    # committed (`SQLITE_BUSY_SNAPSHOT`), which would surface as a spurious 500
    # under exactly the concurrent load this guards against — so the snapshot is
    # ended and the claim insert starts a fresh transaction. Same reasoning as
    # `request_processing()`.
    session.rollback()
    fresh = session.get(Recording, recording_id)
    if fresh is None:
        raise ProcessingError("recording vanished while denoising")

    meta = dict(fresh.processing_meta or {})
    claim = _claim_denoise_slot(session, recording_id, actor)
    # The claim is committed before any file is touched: it has to survive this
    # process dying, otherwise the exhibit is claimed in memory only and the
    # next caller repeats the work the dead one was doing.
    session.commit()

    try:
        source = Path(fresh.working_path or fresh.original_path)
        if not source.exists():
            raise ProcessingError(
                "no readable original or working copy for this recording"
            )

        if not fresh.working_path:
            # Denoise from the canonical mono 16 kHz working copy so the
            # derivative lines up sample-for-sample with the original
            # transcript timeline.
            working = storage.working_path(recording_id)
            try:
                audio_tools.to_working_copy(Path(fresh.original_path), working)
            except (DependencyUnavailable, ProcessingError) as exc:
                # Recorded separately from the denoise itself: "ffmpeg would not
                # make the working copy" and "the denoiser failed" are different
                # things for whoever has to fix it.
                meta["working_copy"] = {"status": "failed", "reason": str(exc)}
                raise
            fresh.working_path = str(working)
            source = working

        canonical = storage.denoised_path(recording_id)
        result = _denoise_atomically(denoiser, source, canonical)
    except Exception as exc:
        # Release the slot, always. A denoise that failed on this input must not
        # leave the exhibit locked until an operator notices and resets it —
        # unlike a crashed process, this one knows it is done, so it says so.
        message = str(exc) or type(exc).__name__
        if meta.get("working_copy", {}).get("status") != "failed":
            # A working-copy failure has already written its own reason; do not
            # overwrite it with the same text under a different key.
            meta["denoise"] = {"status": "failed", "reason": message}
        fresh.processing_meta = meta
        claim.status = JobStatus.FAILED
        claim.stage = "denoise.failed"
        claim.error = message
        claim.finished_at = utcnow()
        session.commit()
        raise

    fresh.denoised_path = str(canonical)
    meta["denoise"] = {"status": "ok", "engine": result.engine, "settings": result.settings}
    fresh.processing_meta = meta

    audit.record(
        session, actor, "recording.denoised",
        entity_type="recording", entity_id=recording_id, recording_id=recording_id,
        detail={
            "engine": result.engine,
            "settings": result.settings,
            "bytes": canonical.stat().st_size if canonical.exists() else None,
            # Names the claim, so a reviewer can tie the artefact to the single
            # call that produced it instead of to a burst of identical rows.
            "job_id": claim.id,
        },
    )
    claim.status = JobStatus.SUCCEEDED
    claim.stage = "denoise.completed"
    claim.progress = 1.0
    claim.finished_at = utcnow()
    session.commit()
    return fresh


def _claim_denoise_slot(session: Session, recording_id: str, actor) -> Job:
    """Take the recording's one active-job slot for an on-demand denoise.

    Insert-only, like `enqueue()`: the partial unique index
    `uq_job_active_recording` decides the winner, so there is no window between
    "is anyone working on this?" and "I am". Losing raises `ActiveJobConflict`,
    which is a *state* conflict — the caller asked for something already true —
    not a failure.

    `status` is set to `running` rather than `queued` deliberately. A queued row
    would be visible to `run_pending_jobs()` and would be executed as a full
    ingest, which is not what the caller asked for.
    """
    job = Job(
        recording_id=recording_id,
        kind="denoise",
        status=JobStatus.RUNNING,
        stage="denoise",
        progress=0.0,
        attempts=1,
        actor=actor.name if hasattr(actor, "name") else str(actor),
        started_at=utcnow(),
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if _is_active_job_conflict(exc):
            raise ActiveJobConflict(recording_id) from exc
        raise
    return job


def _denoise_atomically(denoiser, source: Path, canonical: Path):
    """Denoise into a private scratch file, then publish it with one `os.replace`.

    Returns the `DenoiseResult` re-pointed at `canonical`, because that is the
    path persisted on the recording and served by `/audio?variant=denoised`.
    Scratch files are removed on every path, including failure: the `denoised/`
    directory is shipped inside a disclosure bundle, so stray files are not
    harmless clutter.
    """
    canonical.parent.mkdir(parents=True, exist_ok=True)
    scratch = canonical.with_name(f"{canonical.stem}.{new_id()}{canonical.suffix}")
    produced = scratch
    try:
        result = denoiser.denoise(source, scratch)
        produced = Path(result.path)
        if not produced.exists() or produced.stat().st_size == 0:
            raise ProcessingError("denoiser produced no output")
        # `os.replace` is atomic within one filesystem: a concurrent reader sees
        # either the previous derivative or this complete one, never a partial
        # write. That is what removes the truncation race, not the message.
        os.replace(produced, canonical)
    finally:
        for leftover in {scratch, produced}:
            if leftover != canonical:
                leftover.unlink(missing_ok=True)

    return replace(result, path=canonical)
# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

def run_job(job_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            logger.warning("job %s vanished", job_id)
            return
        if job.status in {JobStatus.SUCCEEDED, JobStatus.RUNNING}:
            return
        recording = session.get(Recording, job.recording_id)
        if recording is None:
            job.status = JobStatus.FAILED
            job.error = "recording no longer exists"
            job.finished_at = utcnow()
            session.commit()
            return

        job.status = JobStatus.RUNNING
        job.started_at = utcnow()
        job.attempts += 1
        job.progress = 0.02
        job.stage = "starting"
        job.error = None
        recording.status = RecordingStatus.PROCESSING
        session.commit()

        try:
            _process(session, job, recording)
        except DependencyUnavailable as exc:
            _fail(session, job, recording, f"{exc.component} unavailable: {exc.reason}")
        except ProcessingError as exc:
            _fail(session, job, recording, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("unexpected processing failure for %s", job.recording_id)
            _fail(session, job, recording, f"{type(exc).__name__}: {exc}")


def _stage(session: Session, job: Job, stage: str, progress: float, detail: dict | None = None) -> None:
    job.stage = stage
    job.progress = round(progress, 3)
    if detail:
        job.detail = {**(job.detail or {}), **detail}
    session.commit()


def _fail(session: Session, job: Job, recording: Recording, message: str) -> None:
    job.status = JobStatus.FAILED
    job.stage = "failed"
    job.error = message
    job.finished_at = utcnow()
    recording.status = RecordingStatus.FAILED
    recording.error = message
    audit.record(
        session, "system", "processing.failed",
        entity_type="recording", entity_id=recording.id, recording_id=recording.id,
        detail={"job_id": job.id, "error": message},
    )
    session.commit()


def _process(session: Session, job: Job, recording: Recording) -> None:
    settings = get_settings()
    warnings: list[str] = []
    meta: dict = dict(recording.processing_meta or {})
    meta["attempt"] = job.attempts

    original = Path(recording.original_path)
    if not original.exists():
        raise ProcessingError("original recording file is missing from storage")

    _clear_previous_attempt(session, recording)

    # --- 1. probe -----------------------------------------------------------
    _stage(session, job, "probing", 0.05)
    try:
        probe = audio_tools.probe(original)
        recording.duration_s = probe.duration_s or recording.duration_s
        recording.sample_rate = probe.sample_rate
        recording.channels = probe.channels
        meta["probe"] = {
            "status": "ok",
            "duration_s": probe.duration_s,
            "sample_rate": probe.sample_rate,
            "channels": probe.channels,
            "codec": probe.codec,
            "container": probe.container,
        }
    except DependencyUnavailable as exc:
        warnings.append(f"{exc.component} unavailable: {exc.reason}")
        meta["probe"] = {"status": "unavailable", "reason": exc.reason}
    except ProcessingError as exc:
        warnings.append(f"probe failed: {exc}")
        meta["probe"] = {"status": "failed", "reason": str(exc)}

    # --- 2. working copy ----------------------------------------------------
    _stage(session, job, "converting", 0.15)
    working = storage.working_path(recording.id)
    try:
        info = audio_tools.to_working_copy(original, working)
        recording.working_path = str(working)
        meta["working_copy"] = {"status": "ok", **info}
    except DependencyUnavailable as exc:
        working = original
        meta["working_copy"] = {"status": "skipped", "reason": exc.reason}
        warnings.append(f"{exc.component} unavailable: {exc.reason}")
    except ProcessingError:
        raise

    # --- 3. denoising -------------------------------------------------------
    _stage(session, job, "denoising", 0.3)
    denoised_path: Path | None = None
    denoiser = get_denoiser(settings.denoiser)
    denoise_ok, denoise_reason = denoiser.available()
    if denoise_ok:
        try:
            # Published atomically, through the same helper as the on-demand
            # path. This stage cannot overlap another denoise (the active-job
            # index guarantees it), but a reader that ignores a half-written
            # derivative is only accidental protection: a bundle zipped while
            # this stage ran used to be able to ship a truncated FLAC. The two
            # call sites now have the same safety argument rather than two.
            canonical = storage.denoised_path(recording.id)
            result = _denoise_atomically(denoiser, working, canonical)
            denoised_path = result.path
            recording.denoised_path = str(result.path)
            meta["denoise"] = {"status": "ok", "engine": result.engine,
                               "settings": result.settings}
        except ProcessingError as exc:
            meta["denoise"] = {"status": "failed", "reason": str(exc)}
            warnings.append(f"denoising failed: {exc}")
    else:
        meta["denoise"] = {"status": "skipped", "reason": denoise_reason}

    # --- 4. transcribe the reference audio ----------------------------------
    _stage(session, job, "transcribing", 0.45)
    transcriber = resolve_transcriber(
        working,
        preference=settings.transcriber,
        model_name=settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        diarize=settings.diarization,
        hf_token=settings.hf_token,
        hint=original,
        language=settings.whisper_language,
    )
    trans_ok, trans_reason = transcriber.available()
    if not trans_ok:
        meta["transcription"] = {"status": "unavailable", "reason": trans_reason}
        warnings.append(f"Transcription unavailable: {trans_reason}")
        meta["warnings"] = warnings
        recording.processing_meta = meta
        recording.duration_s = recording.duration_s
        job.status = JobStatus.SUCCEEDED
        job.stage = "completed_with_warnings"
        job.progress = 1.0
        job.finished_at = utcnow()
        job.detail = {**(job.detail or {}), "warnings": warnings}
        recording.status = RecordingStatus.READY
        recording.error = None
        audit.record(
            session, "system", "processing.completed_with_warnings",
            entity_type="recording", entity_id=recording.id, recording_id=recording.id,
            detail={"job_id": job.id, "warnings": warnings},
        )
        session.commit()
        return

    result = transcriber.transcribe(working, hint=original)
    if not result.segments:
        warnings.append("transcription returned no segments")
    transcript = _store_transcript(session, recording, TranscriptVariant.ORIGINAL, result)
    meta["transcription"] = {
        "status": "ok",
        "engine": result.engine,
        "model_version": result.model_version,
        "language": result.language,
        "segments": len(result.segments),
        "settings": result.settings,
    }
    if recording.duration_s in (None, 0) and result.duration_s:
        recording.duration_s = result.duration_s

    # --- 5. transcribe the denoised derivative ------------------------------
    _stage(session, job, "transcribing_denoised", 0.62)
    if denoised_path is not None:
        try:
            if isinstance(transcriber, SidecarTranscriber):
                reference = _store_transcript(
                    session, recording, TranscriptVariant.DENOISED, result,
                    is_reference_copy=True,
                    note=(
                        "Reference transcript reused for the denoised derivative — "
                        "no ASR engine was available, so this is not an independent "
                        "recognition pass."
                    ),
                )
                meta["denoise_transcription"] = {
                    "status": "reference_copy",
                    "transcript_id": reference.id,
                }
                warnings.append(
                    "Denoised transcript is a reference copy, not an independent ASR pass."
                )
            else:
                denoised_result = transcriber.transcribe(denoised_path, hint=original)
                derivative = _store_transcript(
                    session, recording, TranscriptVariant.DENOISED, denoised_result
                )
                meta["denoise_transcription"] = {
                    "status": "ok",
                    "transcript_id": derivative.id,
                    "engine": denoised_result.engine,
                    "segments": len(denoised_result.segments),
                }
        except Exception as exc:
            meta["denoise_transcription"] = {"status": "failed", "reason": str(exc)}
            warnings.append(f"denoised transcription failed: {exc}")

    # --- 6. embeddings ------------------------------------------------------
    _stage(session, job, "embedding", 0.8)
    embedder = get_embedder(settings.embedder, settings.embedding_model)
    items = _embed_transcript(session, recording, transcript, embedder)
    meta["embedding"] = {
        "status": "ok",
        "model": embedder.name,
        "dim": embedder.dim,
        "semantic": embedder.is_semantic,
        "vectors": len(items),
    }

    store = get_vector_store(
        session,
        preference=settings.vector_store,
        url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        dim=embedder.dim,
    )
    try:
        store.upsert(items)
        ok, reason = store.health()
        meta["vector_store"] = {"status": "ok" if ok else "degraded", "store": store.name,
                                "detail": reason}
    except Exception as exc:  # pragma: no cover - remote store
        meta["vector_store"] = {"status": "failed", "store": store.name, "reason": str(exc)}
        warnings.append(f"vector index unavailable: {exc}")

    # --- 7. finalise --------------------------------------------------------
    meta["warnings"] = warnings
    recording.processing_meta = meta
    recording.status = RecordingStatus.READY
    recording.error = None
    job.status = JobStatus.SUCCEEDED
    job.stage = "completed" if not warnings else "completed_with_warnings"
    job.progress = 1.0
    job.finished_at = utcnow()
    job.detail = {**(job.detail or {}), "warnings": warnings}
    audit.record(
        session, "system", "processing.completed",
        entity_type="recording", entity_id=recording.id, recording_id=recording.id,
        detail={
            "job_id": job.id,
            "segments": len(result.segments),
            "transcriber": result.engine,
            "warnings": warnings,
        },
    )
    session.commit()


# --------------------------------------------------------------------------- #
# persistence helpers
# --------------------------------------------------------------------------- #

def _clear_previous_attempt(session: Session, recording: Recording) -> None:
    """Remove derivatives and transcripts from an earlier attempt (retry path)."""
    for transcript in list(recording.transcripts):
        indexing.drop_transcript(session, transcript.id)
        session.execute(
            SegmentEmbedding.__table__.delete().where(
                SegmentEmbedding.recording_id == recording.id
            )
        )
        session.delete(transcript)
    recording.transcripts.clear()
    session.commit()


def _store_transcript(
    session: Session,
    recording: Recording,
    variant: str,
    result: TranscriptionResult,
    *,
    is_reference_copy: bool = False,
    note: str | None = None,
) -> Transcript:
    settings = dict(result.settings or {})
    if note:
        settings["note"] = note

    transcript = Transcript(
        recording_id=recording.id,
        variant=variant,
        engine=result.engine,
        model_version=result.model_version,
        language=result.language,
        is_reference_copy=is_reference_copy,
        settings=settings,
    )
    session.add(transcript)
    session.flush()

    segments: list[Segment] = []
    for idx, item in enumerate(result.segments):
        segment = Segment(
            transcript_id=transcript.id,
            recording_id=recording.id,
            idx=idx,
            start_s=float(item.get("start", 0.0)),
            end_s=float(item.get("end", 0.0)),
            text=(item.get("text") or "").strip(),
            ai_text=(item.get("text") or "").strip(),
            speaker=item.get("speaker"),
            words=item.get("words") or None,
            review_status=ReviewStatus.AI_GENERATED,
        )
        session.add(segment)
        segments.append(segment)
    session.flush()

    indexing.index_segments(session, segments)
    session.commit()

    # Redaction cues are derived from the original transcript only: they are a
    # projection of the working text, and the denoised pass would duplicate them.
    if variant == TranscriptVariant.ORIGINAL:
        from .. import cues as cue_engine

        cue_engine.scan_recording(session, recording)
        session.commit()

    return transcript


def _embed_transcript(
    session: Session, recording: Recording, transcript: Transcript, embedder
) -> list[VectorItem]:
    segments = list(
        session.execute(
            select(Segment).where(Segment.transcript_id == transcript.id).order_by(Segment.idx)
        ).scalars()
    )
    items: list[VectorItem] = []
    recorded_at = recording.recorded_at
    for offset in range(0, len(segments), EMBED_BATCH):
        chunk = segments[offset:offset + EMBED_BATCH]
        vectors = embedder.embed_documents([s.text for s in chunk])
        for segment, vector in zip(chunk, vectors):
            session.merge(
                SegmentEmbedding(
                    segment_id=segment.id,
                    recording_id=recording.id,
                    model=embedder.name,
                    dim=len(vector),
                    vector=json.dumps(vector),
                )
            )
            items.append(
                VectorItem(
                    segment_id=segment.id,
                    recording_id=recording.id,
                    vector=vector,
                    payload={
                        "recording_id": recording.id,
                        "variant": transcript.variant,
                        "source_type": recording.source_type,
                        "speaker": segment.speaker or "",
                        "review_status": segment.review_status,
                        "recorded_at": int(recorded_at.timestamp()) if recorded_at else 0,
                    },
                )
            )
        session.commit()
    return items
