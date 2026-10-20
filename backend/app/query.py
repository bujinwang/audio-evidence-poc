"""Shared SQL filtering for segment searches (keyword and semantic paths)."""

from __future__ import annotations

from sqlalchemy import Select

from .filters import SearchFilters
from .models import Recording, Segment, Transcript


def apply_segment_filters(stmt: Select, filters: SearchFilters) -> Select:
    stmt = stmt.where(Transcript.variant == (filters.variant or "original"))
    if filters.recording_ids:
        stmt = stmt.where(Segment.recording_id.in_(filters.recording_ids))
    if filters.source_types:
        stmt = stmt.where(Recording.source_type.in_(filters.source_types))
    if filters.speakers:
        stmt = stmt.where(Segment.speaker.in_(filters.speakers))
    if filters.review_statuses:
        stmt = stmt.where(Segment.review_status.in_(filters.review_statuses))
    if filters.date_from is not None:
        stmt = stmt.where(Recording.recorded_at >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(Recording.recorded_at <= filters.date_to)
    return stmt
