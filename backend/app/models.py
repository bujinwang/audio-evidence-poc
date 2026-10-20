"""ORM models.

Design notes that matter for the evidentiary story:

* `Recording.original_path` is written once and never mutated. Derivatives live
  in separate directories and are referenced explicitly.
* `Segment.ai_text` is the immutable machine output. `Segment.text` is the
  current working text. Nothing ever overwrites `ai_text`.
* Every text change appends to `Revision`; every state change appends to
  `AuditEvent`. Both are append-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- enumerated string constants (kept as plain strings: no PG enum migrations) ---

class RecordingStatus:
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    # Registered, hashed and probed, but deliberately NOT transcribed or
    # embedded. Ling's 911 example: a case reopened after ten years needs twenty
    # minutes out of thousands of hours, and a force will not pay to fully index
    # every hour of tape. Triage makes the recording citable and playable at
    # near-zero cost, and promotion to READY happens only when a case needs it.
    TRIAGED = "triaged"


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TranscriptVariant:
    ORIGINAL = "original"
    DENOISED = "denoised"


class ReviewStatus:
    AI_GENERATED = "ai_generated"
    IN_REVIEW = "in_review"
    CORRECTED = "corrected"
    VERIFIED = "verified"

    ALL = (AI_GENERATED, IN_REVIEW, CORRECTED, VERIFIED)


SOURCE_TYPES = ("interview", "bodycam", "cassette", "phone_call", "other")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rec_"))
    filename: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(32), default="other", index=True)
    original_path: Mapped[str] = mapped_column(String(1024))
    working_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    denoised_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=RecordingStatus.UPLOADED, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded_by: Mapped[str] = mapped_column(String(128), default="unknown")
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job_"))
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="process")
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recording: Mapped[Recording] = relationship(back_populates="jobs")


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("recording_id", "variant", name="uq_transcript_variant"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("tr_"))
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    variant: Mapped[str] = mapped_column(String(16), default=TranscriptVariant.ORIGINAL)
    engine: Mapped[str] = mapped_column(String(64), default="unknown")
    model_version: Mapped[str] = mapped_column(String(128), default="unknown")
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_reference_copy: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    recording: Mapped[Recording] = relationship(back_populates="transcripts")
    segments: Mapped[list["Segment"]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        order_by="Segment.idx",
    )


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        Index("ix_segments_transcript_idx", "transcript_id", "idx"),
        Index("ix_segments_recording_start", "recording_id", "start_s"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("seg_"))
    transcript_id: Mapped[str] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), index=True
    )
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    text: Mapped[str] = mapped_column(Text, default="")
    ai_text: Mapped[str] = mapped_column(Text, default="")
    speaker: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    words: Mapped[list | None] = mapped_column(JSON, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(24), default=ReviewStatus.AI_GENERATED, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    verified_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transcript: Mapped[Transcript] = relationship(back_populates="segments")
    revisions: Mapped[list["Revision"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan", order_by="Revision.version"
    )

    @property
    def duration_s(self) -> float:
        return round(max(self.end_s - self.start_s, 0.0), 3)


class Revision(Base):
    __tablename__ = "revisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rev_"))
    segment_id: Mapped[str] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=2)
    prev_text: Mapped[str] = mapped_column(Text, default="")
    new_text: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(24), default="edit")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    segment: Mapped[Segment] = relationship(back_populates="revisions")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("evt_"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(32), default="unknown")
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    recording_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class SegmentEmbedding(Base):
    """Vectors are persisted here regardless of vector-store choice, so the
    semantic index can be rebuilt or migrated without re-running ASR."""

    __tablename__ = "segment_embeddings"

    segment_id: Mapped[str] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True
    )
    recording_id: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(128))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
