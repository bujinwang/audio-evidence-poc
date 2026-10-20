"""ORM → response mapping."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Job, Recording, ReviewStatus, Segment, Transcript, TranscriptVariant
from .schemas import JobOut, RecordingOut, SegmentOut, TranscriptOut


def job_out(job: Job | None) -> JobOut | None:
    if job is None:
        return None
    return JobOut(
        id=job.id,
        status=job.status,
        stage=job.stage,
        progress=round(job.progress or 0.0, 3),
        error=job.error,
        detail=job.detail or {},
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def segment_out(segment: Segment, variant: str = TranscriptVariant.ORIGINAL) -> SegmentOut:
    return SegmentOut(
        id=segment.id,
        recording_id=segment.recording_id,
        transcript_id=segment.transcript_id,
        variant=variant,
        idx=segment.idx,
        start_s=segment.start_s,
        end_s=segment.end_s,
        text=segment.text,
        ai_text=segment.ai_text,
        speaker=segment.speaker,
        review_status=segment.review_status,
        version=segment.version,
        edited=segment.text.strip() != segment.ai_text.strip(),
        verified_by=segment.verified_by,
        verified_at=segment.verified_at,
        words=segment.words,
    )


def transcript_out(session: Session, transcript: Transcript) -> TranscriptOut:
    segments = list(
        session.execute(
            select(Segment)
            .where(Segment.transcript_id == transcript.id)
            .order_by(Segment.idx)
        ).scalars()
    )
    return TranscriptOut(
        id=transcript.id,
        recording_id=transcript.recording_id,
        variant=transcript.variant,
        engine=transcript.engine,
        model_version=transcript.model_version,
        language=transcript.language,
        is_reference_copy=transcript.is_reference_copy,
        settings=transcript.settings or {},
        created_at=transcript.created_at,
        segment_count=len(segments),
        segments=[segment_out(s, transcript.variant) for s in segments],
    )


def recording_out(session: Session, recording: Recording) -> RecordingOut:
    transcripts = list(
        session.execute(
            select(Transcript).where(Transcript.recording_id == recording.id)
        ).scalars()
    )
    variants = [t.variant for t in transcripts]

    counts = {status: 0 for status in ReviewStatus.ALL}
    segment_count = 0
    original = next((t for t in transcripts if t.variant == TranscriptVariant.ORIGINAL), None)
    if original is not None:
        rows = session.execute(
            select(Segment.review_status, func.count(Segment.id))
            .where(Segment.transcript_id == original.id)
            .group_by(Segment.review_status)
        ).all()
        for status_value, count in rows:
            counts[status_value] = int(count)
            segment_count += int(count)

    latest_job = session.execute(
        select(Job).where(Job.recording_id == recording.id).order_by(Job.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    warnings: list[str] = list((recording.processing_meta or {}).get("warnings") or [])

    return RecordingOut(
        id=recording.id,
        filename=recording.filename,
        source_type=recording.source_type,
        sha256=recording.sha256,
        size_bytes=recording.size_bytes,
        duration_s=recording.duration_s,
        sample_rate=recording.sample_rate,
        channels=recording.channels,
        status=recording.status,
        error=recording.error,
        uploaded_by=recording.uploaded_by,
        recorded_at=recording.recorded_at,
        created_at=recording.created_at,
        has_original=bool(recording.original_path),
        has_denoised=bool(recording.denoised_path),
        variants=variants,
        segment_count=segment_count,
        review_counts=counts,
        warnings=warnings,
        processing_meta=recording.processing_meta or {},
        latest_job=job_out(latest_job),
    )
