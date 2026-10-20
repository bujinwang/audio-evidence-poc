"""Request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class JobOut(BaseModel):
    id: str
    status: str
    stage: str
    progress: float
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RecordingOut(BaseModel):
    id: str
    filename: str
    source_type: str
    sha256: str
    size_bytes: int
    duration_s: float | None
    sample_rate: int | None
    channels: int | None
    status: str
    error: str | None
    uploaded_by: str
    recorded_at: datetime | None
    created_at: datetime
    has_original: bool = True
    has_denoised: bool = False
    variants: list[str] = Field(default_factory=list)
    segment_count: int = 0
    review_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    processing_meta: dict[str, Any] = Field(default_factory=dict)
    latest_job: JobOut | None = None


class SegmentOut(BaseModel):
    id: str
    recording_id: str
    transcript_id: str
    variant: str
    idx: int
    start_s: float
    end_s: float
    text: str
    ai_text: str
    speaker: str | None
    review_status: str
    version: int
    edited: bool
    verified_by: str | None
    verified_at: datetime | None
    words: list[dict[str, Any]] | None = None


class TranscriptOut(BaseModel):
    id: str
    recording_id: str
    variant: str
    engine: str
    model_version: str
    language: str | None
    is_reference_copy: bool
    settings: dict[str, Any]
    created_at: datetime
    segment_count: int
    segments: list[SegmentOut] = Field(default_factory=list)


class SearchHit(BaseModel):
    segment_id: str
    recording_id: str
    recording_filename: str
    source_type: str
    recorded_at: datetime | None
    transcript_id: str
    start_s: float
    end_s: float
    text: str
    snippet: str
    speaker: str | None
    review_status: str
    score: float
    keyword_score: float | None = None
    semantic_score: float | None = None
    matched_by: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    mode: str
    took_ms: int
    total: int
    hits: list[SearchHit]
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class SegmentUpdate(BaseModel):
    text: str = Field(min_length=0, max_length=20000)
    note: str | None = None


class VerifyRequest(BaseModel):
    verified: bool = True
    note: str | None = None


class RevisionOut(BaseModel):
    id: str
    version: int
    prev_text: str
    new_text: str
    actor: str
    action: str
    note: str | None
    created_at: datetime


class AuditEventOut(BaseModel):
    id: str
    ts: datetime
    actor: str
    role: str
    action: str
    entity_type: str
    entity_id: str
    recording_id: str | None
    detail: dict[str, Any]


class SegmentHistoryOut(BaseModel):
    segment_id: str
    ai_text: str
    current_text: str
    review_status: str
    revisions: list[RevisionOut]
    events: list[AuditEventOut]


class ComparisonRow(BaseModel):
    idx: int
    start_s: float
    end_s: float
    original_text: str
    denoised_text: str
    speaker: str | None
    similarity: float | None


class ComparisonOut(BaseModel):
    recording_id: str
    available: bool
    reason: str | None = None
    original_engine: str | None = None
    denoised_engine: str | None = None
    denoised_is_reference_copy: bool = False
    rows: list[ComparisonRow] = Field(default_factory=list)


class CapabilityOut(BaseModel):
    name: str
    available: bool
    detail: str
    kind: Literal["core", "optional"] = "optional"


class ProvenanceStep(BaseModel):
    """One link in the chain of custody for a recording."""

    stage: str
    label: str
    status: str
    actor: str | None = None
    role: str | None = None
    at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ProvenanceOut(BaseModel):
    recording_id: str
    filename: str
    sha256: str
    source_type: str
    intake_mode: str
    status: str
    steps: list[ProvenanceStep] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    database: str
    vector_store: str
    embedder: str
    transcriber: str
    denoiser: str
    capabilities: list[CapabilityOut]


class ActorOut(BaseModel):
    actor: str
    role: str
    permissions: list[str]
