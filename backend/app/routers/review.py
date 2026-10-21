"""Human review: correction, verification, revision history.

Rules enforced here (PRD FR-06):

* Editing text never marks a segment verified.
* Editing a verified segment invalidates the verification and records that it
  did so — the audit trail shows the verification was withdrawn, not silently
  overwritten.
* Verification requires an explicit call and a non-empty text.
* `ai_text` is never mutated; corrections only touch `text`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import audit, indexing
from ..db import get_session
from ..models import AuditEvent, Revision, ReviewStatus, Segment, Transcript, utcnow
from ..schemas import (
    AuditEventOut,
    RevisionOut,
    SegmentHistoryOut,
    SegmentOut,
    SegmentUpdate,
    VerifyRequest,
)
from ..security import Actor, require
from ..serializers import segment_out

router = APIRouter(prefix="/api/segments", tags=["review"])


def _get_segment(session: Session, segment_id: str) -> Segment:
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="segment not found")
    return segment


def _variant_of(session: Session, segment: Segment) -> str:
    transcript = session.get(Transcript, segment.transcript_id)
    return transcript.variant if transcript else "original"


def _allocate_version(session: Session, segment: Segment) -> int:
    """Reserve the next revision number for `segment` atomically.

    Reading `segment.version` and adding one in Python is a read-modify-write:
    two concurrent edits both read `1`, both compute `2`, and both insert a
    revision claiming to be version 2. The correction history for that passage
    then holds two different changes under one number — an ambiguous audit
    trail. `UPDATE ... RETURNING` moves the increment into a single statement,
    so the database serialises the two writers and each receives a number no
    other transaction also received. (`RETURNING` is supported by both SQLite
    >= 3.35 and PostgreSQL, the two dialects this app targets.)

    The value is written straight to the row, so the ORM copy is deliberately
    left alone (`synchronize_session=False`): if the attribute were updated from
    Python instead, the next flush could rewrite the column from a value that a
    concurrent transaction had already moved past. Callers must
    `session.refresh(segment)` before serialising.
    """
    table = Segment.__table__
    next_version = session.execute(
        update(table)
        .where(table.c.id == segment.id)
        .values(version=table.c.version + 1)
        .returning(table.c.version),
        execution_options={"synchronize_session": False},
    ).scalar_one_or_none()

    if next_version is None:  # segment vanished between load and update
        raise HTTPException(status_code=404, detail="segment not found")
    return int(next_version)


@router.patch("/{segment_id}", response_model=SegmentOut)
def update_segment(
    segment_id: str,
    payload: SegmentUpdate,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("edit")),
) -> SegmentOut:
    segment = _get_segment(session, segment_id)
    previous_text = segment.text
    new_text = payload.text

    if new_text == previous_text and not payload.note:
        return segment_out(segment, _variant_of(session, segment))

    invalidated_verification = segment.review_status == ReviewStatus.VERIFIED

    # Reserve the version before writing anything else: if this raises, no
    # revision row is written and the transaction rolls back as a whole.
    new_version = _allocate_version(session, segment)

    segment.text = new_text
    segment.review_status = ReviewStatus.CORRECTED
    segment.verified_by = None
    segment.verified_at = None

    session.add(
        Revision(
            segment_id=segment.id,
            version=new_version,
            prev_text=previous_text,
            new_text=new_text,
            actor=actor.name,
            action="edit",
            note=payload.note,
        )
    )
    indexing.reindex_segment(session, segment)

    audit.record(
        session, actor, "segment.edited",
        entity_type="segment", entity_id=segment.id, recording_id=segment.recording_id,
        detail={
            "version": new_version,
            "prev_text": previous_text,
            "new_text": new_text,
            "note": payload.note,
            "invalidated_verification": invalidated_verification,
        },
    )
    if invalidated_verification:
        audit.record(
            session, actor, "segment.verification_invalidated",
            entity_type="segment", entity_id=segment.id, recording_id=segment.recording_id,
            detail={"reason": "text edited after verification", "version": new_version},
        )

    session.commit()
    # `expire_on_commit=False`, so the ORM still holds the pre-update version.
    # Re-read the row to report what the database actually allocated.
    session.refresh(segment)
    return segment_out(segment, _variant_of(session, segment))


@router.post("/{segment_id}/verify", response_model=SegmentOut)
def verify_segment(
    segment_id: str,
    payload: VerifyRequest,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("verify")),
) -> SegmentOut:
    segment = _get_segment(session, segment_id)
    variant = _variant_of(session, segment)

    if payload.verified:
        if not (segment.text or "").strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot verify a segment with empty text.",
            )
        segment.review_status = ReviewStatus.VERIFIED
        segment.verified_by = actor.name
        segment.verified_at = utcnow()
        action = "segment.verified"
    else:
        segment.review_status = (
            ReviewStatus.CORRECTED
            if (segment.text or "").strip() != (segment.ai_text or "").strip()
            else ReviewStatus.AI_GENERATED
        )
        segment.verified_by = None
        segment.verified_at = None
        action = "segment.verification_removed"

    audit.record(
        session, actor, action,
        entity_type="segment", entity_id=segment.id, recording_id=segment.recording_id,
        detail={
            "note": payload.note,
            "version": segment.version,
            "review_status": segment.review_status,
            "start_s": segment.start_s,
            "end_s": segment.end_s,
        },
    )
    session.commit()
    session.refresh(segment)
    return segment_out(segment, variant)


@router.post("/{segment_id}/reopen", response_model=SegmentOut)
def reopen_segment(
    segment_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("edit")),
) -> SegmentOut:
    """Move a segment back into review, withdrawing any verification."""
    segment = _get_segment(session, segment_id)
    variant = _variant_of(session, segment)
    was_verified = segment.review_status == ReviewStatus.VERIFIED

    segment.review_status = ReviewStatus.IN_REVIEW
    segment.verified_by = None
    segment.verified_at = None

    audit.record(
        session, actor, "segment.review_opened",
        entity_type="segment", entity_id=segment.id, recording_id=segment.recording_id,
        detail={"previous_status": ReviewStatus.VERIFIED if was_verified else "other",
                "invalidated_verification": was_verified},
    )
    session.commit()
    session.refresh(segment)
    return segment_out(segment, variant)


@router.get("/{segment_id}/history", response_model=SegmentHistoryOut)
def segment_history(
    segment_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> SegmentHistoryOut:
    segment = _get_segment(session, segment_id)

    revisions = list(
        session.execute(
            select(Revision).where(Revision.segment_id == segment_id).order_by(Revision.version)
        ).scalars()
    )
    events = list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.entity_id == segment_id)
            .order_by(AuditEvent.ts)
        ).scalars()
    )

    return SegmentHistoryOut(
        segment_id=segment_id,
        ai_text=segment.ai_text,
        current_text=segment.text,
        review_status=segment.review_status,
        revisions=[
            RevisionOut(
                id=r.id, version=r.version, prev_text=r.prev_text, new_text=r.new_text,
                actor=r.actor, action=r.action, note=r.note, created_at=r.created_at,
            )
            for r in revisions
        ],
        events=[
            AuditEventOut(
                id=e.id, ts=e.ts, actor=e.actor, role=e.role, action=e.action,
                entity_type=e.entity_type, entity_id=e.entity_id,
                recording_id=e.recording_id, detail=e.detail or {},
            )
            for e in events
        ],
    )
