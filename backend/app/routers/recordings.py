"""Audio library: upload, list, inspect, retry, play, compare."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, storage
from ..config import get_settings
from ..db import get_session
from ..errors import DependencyUnavailable, ProcessingError
from ..media import audio_response
from ..models import (
    Job,
    Recording,
    Segment,
    Transcript,
    TranscriptVariant,
    new_id,
)
from ..pipeline import audio as audio_tools
from ..pipeline import jobs
from ..schemas import (
    ComparisonOut,
    ComparisonRow,
    JobOut,
    ProvenanceOut,
    ProvenanceStep,
    RecordingOut,
    TranscriptOut,
)
from ..security import Actor, require
from ..serializers import job_out, recording_out, transcript_out

router = APIRouter(prefix="/api/recordings", tags=["recordings"])


def _parse_recorded_at(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.post("", response_model=RecordingOut, status_code=status.HTTP_201_CREATED)
async def upload_recording(
    file: UploadFile = File(...),
    source_type: str = Form("other"),
    recorded_at: str | None = Form(None),
    transcript_file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("upload")),
) -> RecordingOut:
    return await _ingest(
        file=file,
        source_type=source_type,
        recorded_at=recorded_at,
        transcript_file=transcript_file,
        mode="full",
        session=session,
        actor=actor,
    )


async def _ingest(
    *,
    file: UploadFile,
    source_type: str,
    recorded_at: str | None,
    transcript_file: UploadFile | None,
    mode: str,
    session: Session,
    actor: Actor,
) -> RecordingOut:
    """Write the original once, hash it, then either triage or fully process."""
    settings = get_settings()
    filename = storage.safe_filename(file.filename or "recording")

    if not storage.is_supported_audio(filename):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio type '{Path(filename).suffix}'. "
                   f"Accepted: {', '.join(sorted(storage.AUDIO_EXTENSIONS))}",
        )

    recording_id = new_id("rec_")
    destination = storage.original_path(recording_id, filename)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0

    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds MAX_UPLOAD_MB={settings.max_upload_mb}.",
                    )
                digest.update(chunk)
                handle.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"upload failed: {exc}") from exc

    if size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty file")

    storage.lock_original_readonly(destination)
    sha256 = digest.hexdigest()

    # Optional reference transcript (used by the sidecar provider when no ASR
    # engine is installed, and for evaluating ASR against reviewed material).
    if transcript_file is not None:
        raw_bytes = await transcript_file.read()
        try:
            json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"reference transcript is not valid JSON: {exc}",
            ) from exc
        storage.sidecar_target(destination).write_bytes(raw_bytes)

    duplicate = session.execute(
        select(Recording).where(Recording.sha256 == sha256).limit(1)
    ).scalar_one_or_none()

    recording = Recording(
        id=recording_id,
        filename=filename,
        source_type=source_type if source_type in {
            "interview", "bodycam", "cassette", "phone_call", "other"
        } else "other",
        original_path=str(destination),
        sha256=sha256,
        size_bytes=size,
        uploaded_by=actor.name,
        recorded_at=_parse_recorded_at(recorded_at),
        processing_meta={"duplicate_of": duplicate.id} if duplicate else {},
    )
    session.add(recording)
    session.commit()

    audit.record(
        session, actor, "recording.uploaded",
        entity_type="recording", entity_id=recording.id, recording_id=recording.id,
        detail={
            "filename": filename,
            "bytes": size,
            "sha256": sha256,
            "source_type": recording.source_type,
            "duplicate_of": duplicate.id if duplicate else None,
        },
    )
    session.commit()

    if mode == "triage":
        jobs.triage(session, recording, actor.name)
        session.expire_all()
        return recording_out(session, session.get(Recording, recording.id))

    jobs.submit(session, recording, actor.name)
    session.expire_all()
    refreshed = session.get(Recording, recording.id)
    return recording_out(session, refreshed)


@router.post("/triage", response_model=RecordingOut, status_code=status.HTTP_201_CREATED)
async def upload_for_triage(
    file: UploadFile = File(...),
    source_type: str = Form("other"),
    recorded_at: str | None = Form(None),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("upload")),
) -> RecordingOut:
    """Register a recording without transcribing or indexing it.

    The cheap end of the ingest cost curve: hash, probe, chain-of-custody. A
    force holding decades of tape can make every hour citable and playable
    without paying for a full ASR + embedding pass it may never need.
    """
    return await _ingest(
        file=file,
        source_type=source_type,
        recorded_at=recorded_at,
        transcript_file=None,
        mode="triage",
        session=session,
        actor=actor,
    )


@router.get("", response_model=list[RecordingOut])
def list_recordings(
    status_filter: str | None = Query(None, alias="status"),
    source_type: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> list[RecordingOut]:
    stmt = select(Recording).order_by(Recording.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Recording.status == status_filter)
    if source_type:
        stmt = stmt.where(Recording.source_type == source_type)
    if q:
        stmt = stmt.where(Recording.filename.ilike(f"%{q}%"))
    recordings = list(session.execute(stmt).scalars())
    return [recording_out(session, r) for r in recordings]


@router.get("/{recording_id}", response_model=RecordingOut)
def get_recording(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> RecordingOut:
    recording = _get_or_404(session, recording_id)
    return recording_out(session, recording)


@router.get("/{recording_id}/jobs", response_model=list[JobOut])
def list_jobs(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> list[JobOut]:
    _get_or_404(session, recording_id)
    rows = session.execute(
        select(Job).where(Job.recording_id == recording_id).order_by(Job.created_at.desc())
    ).scalars()
    return [job_out(job) for job in rows if job is not None]


@router.post("/{recording_id}/retry", response_model=RecordingOut)
def retry_processing(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("retry")),
) -> RecordingOut:
    recording = _get_or_404(session, recording_id)
    locked_reason = jobs.review_locked(recording_id, session)
    if locked_reason:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=locked_reason)

    running = session.execute(
        select(Job)
        .where(Job.recording_id == recording_id)
        .where(Job.status.in_(["queued", "running"]))
        .limit(1)
    ).scalar_one_or_none()
    if running is not None:
        raise HTTPException(status_code=409, detail="A processing job is already queued or running.")

    audit.record(
        session, actor, "recording.processing_requested",
        entity_type="recording", entity_id=recording_id, recording_id=recording_id,
        detail={"previous_status": recording.status, "previous_error": recording.error},
    )
    session.commit()
    jobs.submit(session, recording, actor.name)
    session.expire_all()
    return recording_out(session, session.get(Recording, recording_id))


@router.post("/{recording_id}/denoise", response_model=RecordingOut)
def denoise_recording(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("retry")),
) -> RecordingOut:
    """Produce the denoised derivative on demand.

    Denoising normally happens once, during ingest. This makes it reachable
    afterwards so an investigator can A/B a passage immediately instead of
    re-running the whole pipeline.
    """
    recording = _get_or_404(session, recording_id)

    running = session.execute(
        select(Job)
        .where(Job.recording_id == recording_id)
        .where(Job.status.in_(["queued", "running"]))
        .limit(1)
    ).scalar_one_or_none()
    if running is not None:
        raise HTTPException(
            status_code=409, detail="A processing job is already queued or running."
        )

    try:
        jobs.denoise_existing(session, recording, actor)
    except DependencyUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Denoising unavailable: {exc.reason}",
        ) from exc
    except ProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    session.expire_all()
    return recording_out(session, session.get(Recording, recording_id))


@router.get("/{recording_id}/provenance", response_model=ProvenanceOut)
def get_provenance(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> ProvenanceOut:
    """Chain of custody for one recording, oldest first.

    Mirrors what Technology North's digitisation process records per box: who
    performed which task on each file, and when, at every stage.
    """
    recording = _get_or_404(session, recording_id)
    meta = recording.processing_meta or {}
    events = audit.list_events(session, recording_id=recording_id, limit=500)
    events.sort(key=lambda e: e.ts)

    # Chain of custody is the handling history of the file, not the full access
    # log. Playback and search events are deliberately excluded — a single
    # investigation generates hundreds of them and they would bury the stages
    # that actually changed the artefact. Repeats of the same stage collapse
    # into one entry with a count, so verifying forty segments reads as one line.
    steps: list[ProvenanceStep] = []
    seen: set[str] = set()
    for event in events:
        if event.action not in _PROVENANCE_LABELS:
            continue
        # Keep the first occurrence of the one-shot stages.
        if event.action in _PROVENANCE_ONE_SHOT:
            if event.action in seen:
                continue
            seen.add(event.action)
        if steps and steps[-1].stage == event.action:
            previous = steps[-1]
            count = int(previous.detail.get("count", 1)) + 1
            steps[-1] = previous.model_copy(
                update={
                    "at": event.ts,
                    "detail": {**(previous.detail or {}), "count": count},
                }
            )
            continue
        steps.append(
            ProvenanceStep(
                stage=event.action,
                label=_PROVENANCE_LABELS[event.action],
                status="done",
                actor=event.actor,
                role=event.role,
                at=event.ts,
                detail=dict(event.detail or {}),
            )
        )

    intake_mode = (meta.get("intake") or {}).get("mode")
    if intake_mode is None:
        intake_mode = "full" if meta.get("transcription") else "triage"
    elif intake_mode == "triage" and any(
        step.stage in {"processing.completed", "processing.completed_with_warnings"}
        for step in steps
    ):
        # Received cheaply, then promoted when a case needed it searchable.
        intake_mode = "triage_promoted"

    return ProvenanceOut(
        recording_id=recording.id,
        filename=recording.filename,
        sha256=recording.sha256,
        source_type=recording.source_type,
        intake_mode=intake_mode,
        status=recording.status,
        steps=steps,
    )


# Human-readable labels for the custody chain. An action listed here is part of
# the handling history; anything absent (playback, search) is excluded.
_PROVENANCE_LABELS = {
    "recording.uploaded": "Received and hashed",
    "recording.triaged": "Triaged — registered without full indexing",
    "recording.denoised": "Denoised derivative produced",
    "recording.processing_requested": "Full processing requested",
    "processing.completed": "Transcription and indexing completed",
    "processing.completed_with_warnings": "Completed with warnings",
    "processing.failed": "Processing failed",
    "audio.clip_exported": "Passage exported",
    "segment.verified": "Segment verified",
    "segment.edited": "Transcript corrected",
    "segment.reopened": "Segment reopened",
    "semantic.reindex": "Semantic index rebuilt",
}

# Stages that happen once per ingest; later repeats are noise.
_PROVENANCE_ONE_SHOT = frozenset({
    "recording.uploaded",
    "recording.triaged",
    "processing.completed",
    "processing.completed_with_warnings",
    "processing.failed",
    "semantic.reindex",
})


@router.get("/{recording_id}/audio")
def get_audio(
    recording_id: str,
    request: Request,
    variant: str = Query("original", pattern="^(original|denoised|working)$"),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> Response:
    recording = _get_or_404(session, recording_id)
    path_map = {
        "original": recording.original_path,
        "denoised": recording.denoised_path,
        "working": recording.working_path,
    }
    raw = path_map.get(variant)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No '{variant}' audio for this recording.",
        )
    path = Path(raw)
    audit.record(
        session, actor, "audio.played",
        entity_type="recording", entity_id=recording_id, recording_id=recording_id,
        detail={"variant": variant, "range": request.headers.get("range")},
    )
    session.commit()
    return audio_response(request, path, download_name=f"{recording_id}-{variant}{path.suffix}")


@router.get("/{recording_id}/transcript", response_model=TranscriptOut)
def get_transcript(
    recording_id: str,
    variant: str = Query("original", pattern="^(original|denoised)$"),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> TranscriptOut:
    _get_or_404(session, recording_id)
    transcript = session.execute(
        select(Transcript)
        .where(Transcript.recording_id == recording_id)
        .where(Transcript.variant == variant)
    ).scalar_one_or_none()
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No '{variant}' transcript for this recording.",
        )
    return transcript_out(session, transcript)


@router.get("/{recording_id}/comparison", response_model=ComparisonOut)
def compare_variants(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> ComparisonOut:
    recording = _get_or_404(session, recording_id)
    transcripts = {
        t.variant: t
        for t in session.execute(
            select(Transcript).where(Transcript.recording_id == recording_id)
        ).scalars()
    }
    original = transcripts.get(TranscriptVariant.ORIGINAL)
    denoised = transcripts.get(TranscriptVariant.DENOISED)

    if original is None:
        return ComparisonOut(
            recording_id=recording_id, available=False,
            reason="No original transcript — transcription has not run or failed.",
        )
    if denoised is None:
        reason = "Denoised derivative not produced."
        denoise_meta = (recording.processing_meta or {}).get("denoise") or {}
        if denoise_meta.get("reason"):
            reason = f"Denoised derivative not produced: {denoise_meta['reason']}"
        return ComparisonOut(
            recording_id=recording_id, available=False, reason=reason,
            original_engine=original.engine,
        )

    orig_segments = _segments(session, original.id)
    den_segments = _segments(session, denoised.id)
    by_index = {s.idx: s for s in den_segments}

    rows: list[ComparisonRow] = []
    for segment in orig_segments:
        other = by_index.get(segment.idx)
        other_text = other.text if other else ""
        similarity = (
            round(SequenceMatcher(None, segment.text.lower(), other_text.lower()).ratio(), 3)
            if other_text
            else None
        )
        rows.append(
            ComparisonRow(
                idx=segment.idx,
                start_s=segment.start_s,
                end_s=segment.end_s,
                original_text=segment.text,
                denoised_text=other_text,
                speaker=segment.speaker,
                similarity=similarity,
            )
        )

    return ComparisonOut(
        recording_id=recording_id,
        available=True,
        original_engine=original.engine,
        denoised_engine=denoised.engine,
        denoised_is_reference_copy=denoised.is_reference_copy,
        rows=rows,
    )


@router.get("/{recording_id}/clip")
def export_clip(
    recording_id: str,
    start: float = Query(..., ge=0),
    end: float | None = Query(None, gt=0),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("export")),
) -> Response:
    """Export a passage cut from the original recording (never re-encoded from a derivative)."""
    recording = _get_or_404(session, recording_id)
    duration = (end - start) if end else 10.0
    if duration <= 0:
        raise HTTPException(status_code=400, detail="end must be greater than start")
    destination = get_settings().data_dir / "exports" / f"{recording_id}_{start:.2f}_{duration:.2f}.flac"
    try:
        audio_tools.make_clip(Path(recording.original_path), destination, start, duration)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"clip export unavailable: {exc}") from exc

    audit.record(
        session, actor, "audio.clip_exported",
        entity_type="recording", entity_id=recording_id, recording_id=recording_id,
        detail={"start_s": start, "duration_s": duration},
    )
    session.commit()
    from fastapi.responses import FileResponse

    return FileResponse(destination, media_type="audio/flac", filename=destination.name)


def _segments(session: Session, transcript_id: str) -> list[Segment]:
    return list(
        session.execute(
            select(Segment).where(Segment.transcript_id == transcript_id).order_by(Segment.idx)
        ).scalars()
    )


def _get_or_404(session: Session, recording_id: str) -> Recording:
    recording = session.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="recording not found")
    return recording
