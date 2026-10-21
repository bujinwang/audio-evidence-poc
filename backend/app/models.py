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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import ACTIVE_JOB_PREDICATE, ACTIVE_JOB_UNIQUE_INDEX, Base


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


SOURCE_TYPES = (
    "interview",
    "bodycam",
    "cassette",
    "phone_call",
    "sports",
    "speech",
    "other",
)


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
    # Every exhibit belongs to a case. Nullable only so pre-existing rows migrate.
    case_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="recording", cascade="all, delete-orphan"
    )


class Job(Base):
    __tablename__ = "jobs"
    # One recording has at most ONE active (queued or running) job.
    #
    # `POST /api/recordings/{id}/retry` used to read "is a job active?" and then
    # insert one. Two concurrent callers both read "no" before either committed
    # and both inserted, so one recording could be transcribed and embedded four
    # times over the same rows — and because `run_job()` skips a job it finds in
    # `running`, three of those four were left in `running` forever, which then
    # made every later retry answer 409 and the exhibit permanently
    # un-reprocessable. A Python-level check cannot close that window; a partial
    # unique index can, and it expresses the invariant exactly: "at most one
    # active job per recording", not "at most one job per recording", because
    # finished jobs must accumulate as audit history.
    #
    # Honoured by both dialects: SQLite supports `CREATE UNIQUE INDEX ... WHERE`
    # (3.8.0+), PostgreSQL supports partial indexes natively. The condition is
    # shared with the migration in `db._ensure_active_job_uniqueness()` so a
    # database created by `create_all` and one upgraded in place end up with the
    # same rule.
    __table_args__ = (
        Index(
            ACTIVE_JOB_UNIQUE_INDEX,
            "recording_id",
            unique=True,
            sqlite_where=text(ACTIVE_JOB_PREDICATE),
            postgresql_where=text(ACTIVE_JOB_PREDICATE),
        ),
    )

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
    # A passage has at most one revision per version number. Without this the
    # audit trail can hold two different corrections both claiming to be
    # "version 2", so the correction history for that passage becomes
    # ambiguous — a chain-of-custody defect, not a cosmetic one. The unique
    # constraint makes the invariant a property of the database rather than
    # something the application merely hopes for. `update_segment()` allocates
    # the number atomically (`UPDATE ... RETURNING`), so a legitimate
    # concurrent edit can never collide here.
    __table_args__ = (
        UniqueConstraint("segment_id", "version", name="uq_revision_segment_version"),
    )

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


class CaseStatus:
    OPEN = "open"
    CLOSED = "closed"


class Case(Base):
    """A case file: the unit a police buyer actually works in.

    The app shipped for a year with recordings and no case, which is why the UI
    could not answer "what is due". Everything evidentiary hangs off this row:
    the exhibit list, the disclosure clock, and the bundle.
    """

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("case_"))
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    subject: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    disclosure_due: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=CaseStatus.OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CueDecision:
    OPEN = "open"
    ACCEPT = "accept"
    REJECT = "reject"
    NOT_REDACTABLE = "not_redactable"

    ALL = (OPEN, ACCEPT, REJECT, NOT_REDACTABLE)


class CueCandidate(Base):
    """A span the redaction pass should look at, with a reason that survives disclosure.

    The application never blurs audio. It produces the worksheet that drives the
    blur pass in the operator's own tool, so the decision and its reason are the
    product, not the edit.
    """

    __tablename__ = "cue_candidates"
    __table_args__ = (Index("ix_cues_recording_start", "recording_id", "start_s"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cue_"))
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    segment_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    span_text: Mapped[str] = mapped_column(Text, default="")
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    reason_code: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    detector: Mapped[str] = mapped_column(String(32), default="rules")
    decision: Mapped[str] = mapped_column(String(16), default=CueDecision.OPEN, index=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
