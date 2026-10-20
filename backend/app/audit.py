"""Append-only audit trail."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, utcnow
from .security import Actor


def record(
    session: Session,
    actor: Actor | str,
    action: str,
    *,
    entity_type: str = "",
    entity_id: str = "",
    recording_id: str | None = None,
    detail: dict | None = None,
) -> AuditEvent:
    if isinstance(actor, Actor):
        name, role = actor.name, actor.role
    else:
        name, role = str(actor), "system"
    event = AuditEvent(
        ts=utcnow(),
        actor=name,
        role=role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        recording_id=recording_id,
        detail=detail or {},
    )
    session.add(event)
    return event


def list_events(
    session: Session,
    *,
    recording_id: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.ts.desc()).limit(limit)
    if recording_id:
        stmt = stmt.where(AuditEvent.recording_id == recording_id)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    return list(session.execute(stmt).scalars())
