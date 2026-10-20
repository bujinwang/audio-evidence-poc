"""Audit trail read endpoints."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_session
from ..schemas import AuditEventOut
from ..security import Actor, require

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _to_out(event) -> AuditEventOut:
    return AuditEventOut(
        id=event.id, ts=event.ts, actor=event.actor, role=event.role, action=event.action,
        entity_type=event.entity_type, entity_id=event.entity_id,
        recording_id=event.recording_id, detail=event.detail or {},
    )


@router.get("", response_model=list[AuditEventOut])
def list_audit_events(
    recording_id: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("audit")),
) -> list[AuditEventOut]:
    events = audit.list_events(
        session, recording_id=recording_id, entity_id=entity_id, action=action, limit=limit
    )
    return [_to_out(e) for e in events]


@router.get("/export.csv")
def export_audit_csv(
    recording_id: str | None = None,
    limit: int = Query(5000, ge=1, le=50000),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("audit")),
) -> StreamingResponse:
    events = audit.list_events(session, recording_id=recording_id, limit=limit)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp_utc", "actor", "role", "action", "entity_type",
                     "entity_id", "recording_id", "detail"])
    for event in events:
        writer.writerow([
            event.ts.isoformat() if event.ts else "",
            event.actor, event.role, event.action, event.entity_type,
            event.entity_id, event.recording_id or "", event.detail or {},
        ])
    buffer.seek(0)

    filename = f"audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    audit.record(
        session, actor, "audit.exported",
        entity_type="audit", entity_id="", recording_id=recording_id,
        detail={"rows": len(events)},
    )
    session.commit()

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
