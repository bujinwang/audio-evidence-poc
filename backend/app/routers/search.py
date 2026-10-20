"""Investigator search: keyword, semantic, hybrid."""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import search as search_service
from ..config import get_settings
from ..db import get_session
from ..filters import SearchFilters
from ..models import Recording, ReviewStatus, Segment, Transcript
from ..schemas import SearchResponse
from ..security import Actor, require

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=SearchResponse)
def search_segments(
    q: str = Query(..., min_length=1, description="Natural-language or keyword query"),
    mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
    limit: int = Query(20, ge=1, le=100),
    recording_id: list[str] | None = Query(None),
    source_type: list[str] | None = Query(None),
    speaker: list[str] | None = Query(None),
    review_status: list[str] | None = Query(None),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    variant: str = Query("original", pattern="^(original|denoised)$"),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> SearchResponse:
    for status_value in review_status or []:
        if status_value not in ReviewStatus.ALL:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422,
                detail=f"Unknown review_status '{status_value}'. Valid: {list(ReviewStatus.ALL)}",
            )

    filters = SearchFilters(
        recording_ids=recording_id or [],
        source_types=source_type or [],
        speakers=speaker or [],
        review_statuses=review_status or [],
        date_from=date_from,
        date_to=date_to,
        variant=variant,
    )

    started = time.perf_counter()
    hits, diagnostics = search_service.search(
        session, q, mode=mode, filters=filters, limit=limit
    )
    took_ms = int((time.perf_counter() - started) * 1000)

    return SearchResponse(
        query=q,
        mode=mode,
        took_ms=took_ms,
        total=len(hits),
        hits=hits,
        diagnostics=diagnostics,
    )


@router.get("/options")
def search_options(
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> dict:
    """Values for the filter controls, limited to what is actually indexed."""
    recordings = session.execute(
        select(Recording.id, Recording.filename, Recording.source_type, Recording.recorded_at)
        .order_by(Recording.created_at.desc())
    ).all()
    speakers = [
        row[0]
        for row in session.execute(
            select(Segment.speaker)
            .join(Transcript, Transcript.id == Segment.transcript_id)
            .where(Transcript.variant == "original")
            .where(Segment.speaker.is_not(None))
            .group_by(Segment.speaker)
            .order_by(func.count(Segment.id).desc())
            .limit(50)
        ).all()
        if row[0]
    ]
    return {
        "recordings": [
            {
                "id": rid,
                "filename": filename,
                "source_type": source_type,
                "recorded_at": recorded_at.isoformat() if recorded_at else None,
            }
            for rid, filename, source_type, recorded_at in recordings
        ],
        "source_types": sorted({row[2] for row in recordings}),
        "speakers": speakers,
        "review_statuses": list(ReviewStatus.ALL),
        "modes": ["keyword", "semantic", "hybrid"],
        "default_limit": get_settings().search_default_limit,
    }
