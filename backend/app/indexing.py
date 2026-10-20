"""Keyword index maintenance.

PostgreSQL uses an expression GIN index on `to_tsvector('english', text)` that
the database keeps in sync automatically. SQLite uses an FTS5 table that the
application updates explicitly whenever segment text changes — including human
corrections, so search always reflects the reviewed text.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Segment

_IS_SQLITE = get_settings().is_sqlite

_INSERT = text("INSERT INTO segments_fts (segment_id, text) VALUES (:segment_id, :text)")
_DELETE = text("DELETE FROM segments_fts WHERE segment_id = :segment_id")
_DELETE_TRANSCRIPT = text(
    "DELETE FROM segments_fts WHERE segment_id IN "
    "(SELECT id FROM segments WHERE transcript_id = :transcript_id)"
)


def index_segment(session: Session, segment: Segment) -> None:
    if not _IS_SQLITE:
        return
    session.execute(_INSERT, {"segment_id": segment.id, "text": segment.text or ""})


def index_segments(session: Session, segments: list[Segment]) -> None:
    if not _IS_SQLITE:
        return
    for segment in segments:
        index_segment(session, segment)


def reindex_segment(session: Session, segment: Segment) -> None:
    """Call after any text change."""
    if not _IS_SQLITE:
        return
    session.execute(_DELETE, {"segment_id": segment.id})
    session.execute(_INSERT, {"segment_id": segment.id, "text": segment.text or ""})


def drop_transcript(session: Session, transcript_id: str) -> None:
    if not _IS_SQLITE:
        return
    session.execute(_DELETE_TRANSCRIPT, {"transcript_id": transcript_id})


def rebuild(session: Session) -> int:
    if not _IS_SQLITE:
        return 0
    session.execute(text("DELETE FROM segments_fts"))
    rows = session.execute(text("SELECT id, text FROM segments")).all()
    for segment_id, segment_text in rows:
        session.execute(_INSERT, {"segment_id": segment_id, "text": segment_text or ""})
    return len(rows)
