"""Asynchronous processing orchestration.

One job == one full ingest of one recording. Progress is committed after every
stage so the library screen can poll a live percentage, and every stage records
either what it did or why it could not (PRD FR-02, acceptance criterion 3).

Retry policy: re-processing is refused if any segment has been human-edited or
verified, because wiping transcripts would destroy review history. The API
surfaces that as a 409 with the reason.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, indexing, storage
from ..config import get_settings
from ..db import SessionLocal
from ..errors import DependencyUnavailable, ProcessingError
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

def enqueue(session: Session, recording: Recording, actor: str, kind: str = "process") -> Job:
    job = Job(recording_id=recording.id, kind=kind, status=JobStatus.QUEUED, actor=actor)
    session.add(job)
    recording.status = RecordingStatus.UPLOADED
    recording.error = None
    session.commit()
    return job


def submit(session: Session, recording: Recording, actor: str) -> Job:
    """Enqueue, and process inline when configured (single-process demo mode)."""
    job = enqueue(session, recording, actor)
    if get_settings().process_inline:
        run_job(job.id)
        session.expire_all()
    return job


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
    """
    settings = get_settings()
    meta: dict = dict(recording.processing_meta or {})

    source = Path(recording.working_path or recording.original_path)
    if not source.exists():
        raise ProcessingError("no readable original or working copy for this recording")

    if not recording.working_path:
        # Denoise from the canonical mono 16 kHz working copy so the derivative
        # lines up sample-for-sample with the original transcript timeline.
        working = storage.working_path(recording.id)
        try:
            audio_tools.to_working_copy(Path(recording.original_path), working)
            recording.working_path = str(working)
            source = working
        except (DependencyUnavailable, ProcessingError) as exc:
            meta["working_copy"] = {"status": "failed", "reason": str(exc)}
            recording.processing_meta = meta
            session.commit()
            raise

    denoiser = get_denoiser(settings.denoiser)
    ok, reason = denoiser.available()
    if not ok:
        meta["denoise"] = {"status": "skipped", "reason": reason}
        recording.processing_meta = meta
        session.commit()
        raise DependencyUnavailable("denoiser", reason)

    result = denoiser.denoise(source, storage.denoised_path(recording.id))
    recording.denoised_path = str(result.path)
    meta["denoise"] = {"status": "ok", "engine": result.engine, "settings": result.settings}
    recording.processing_meta = meta

    audit.record(
        session, actor, "recording.denoised",
        entity_type="recording", entity_id=recording.id, recording_id=recording.id,
        detail={
            "engine": result.engine,
            "settings": result.settings,
            "bytes": result.path.stat().st_size if result.path.exists() else None,
        },
    )
    session.commit()
    return recording
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
            result = denoiser.denoise(working, storage.denoised_path(recording.id))
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
