"""Cases, the redaction cue sheet, the cost model, and the disclosure bundle."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import case as sa_case, func, select
from sqlalchemy.orm import Session

from .. import audit, bundle, cues
from ..db import get_session
from ..models import (
    AuditEvent,
    Case,
    CueCandidate,
    CueDecision,
    Recording,
    ReviewStatus,
    Segment,
    Transcript,
    TranscriptVariant,
)
from ..schemas import (
    BundleItemOut,
    BundlePlanOut,
    CaseOut,
    CostOut,
    CostScenarioOut,
    CueDecisionIn,
    CueOut,
    ExhibitOut,
)
from ..security import Actor, require

router = APIRouter(prefix="/api", tags=["cases"])

# List price per audio hour for ASR compute, and the measured real-time factor of
# the default model on this machine. Both are shown in the UI as assumptions.
RATE_PER_HOUR = 0.36
RTF = 0.15
SPOTTING_RTF = 0.02
SPOTTING_WINNER_SHARE = 0.14
WINDOW_SECONDS = 240.0


def _days_to(due: datetime | None) -> int | None:
    if due is None:
        return None
    now = datetime.now(timezone.utc)
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (due - now).days


def _original_transcript(session: Session, recording_id: str) -> Transcript | None:
    return session.execute(
        select(Transcript)
        .where(
            Transcript.recording_id == recording_id,
            Transcript.variant == TranscriptVariant.ORIGINAL,
        )
        .limit(1)
    ).scalar_one_or_none()


def _cue_counts(session: Session, recording_ids: list[str]) -> dict[str, tuple[int, int]]:
    if not recording_ids:
        return {}
    rows = session.execute(
        select(
            CueCandidate.recording_id,
            func.count(CueCandidate.id),
            func.sum(sa_case((CueCandidate.decision == CueDecision.OPEN, 1), else_=0)),
        )
        .where(CueCandidate.recording_id.in_(recording_ids))
        .group_by(CueCandidate.recording_id)
    ).all()
    return {rec_id: (int(total), int(open_count or 0)) for rec_id, total, open_count in rows}


def _custody_counts(session: Session, recording_ids: list[str]) -> dict[str, int]:
    if not recording_ids:
        return {}
    rows = session.execute(
        select(AuditEvent.recording_id, func.count(AuditEvent.id))
        .where(AuditEvent.recording_id.in_(recording_ids))
        .group_by(AuditEvent.recording_id)
    ).all()
    return {rec_id: int(count) for rec_id, count in rows if rec_id}


def _exhibit(session: Session, recording: Recording, index: int) -> ExhibitOut:
    counts = {value: 0 for value in ReviewStatus.ALL}
    total = 0
    transcript = _original_transcript(session, recording.id)
    if transcript is not None:
        for status_value, count in session.execute(
            select(Segment.review_status, func.count(Segment.id))
            .where(Segment.transcript_id == transcript.id)
            .group_by(Segment.review_status)
        ).all():
            counts[status_value] = int(count)
            total += int(count)

    duration = float(recording.duration_s or 0.0)
    indexed = "full" if transcript is not None else "none"
    cue_total, cue_open = _cue_counts(session, [recording.id]).get(recording.id, (0, 0))
    custody = _custody_counts(session, [recording.id]).get(recording.id, 0)
    verified = counts.get(ReviewStatus.VERIFIED, 0)

    return ExhibitOut(
        id=recording.id,
        filename=recording.filename,
        source_type=recording.source_type,
        duration_s=recording.duration_s,
        recorded_at=recording.recorded_at,
        status=recording.status,
        segment_count=total,
        review_counts=counts,
        reviewed_pct=round(verified / total * 100, 1) if total else 0.0,
        indexed=indexed,
        indexed_ranges=[[0.0, round(duration, 3)]] if indexed == "full" else [],
        indexed_s=round(duration, 3) if indexed == "full" else 0.0,
        dark_s=0.0 if indexed == "full" else round(duration, 3),
        custody_events=custody,
        cue_total=cue_total,
        cue_open=cue_open,
        # Truthiness of the stored path, not a file-existence check: the column
        # is what the audio endpoint itself reads to decide whether to serve or
        # 404, so the switch and the endpoint can never disagree.
        has_denoised=bool(recording.denoised_path),
    )


def _case_out(session: Session, case: Case, with_exhibits: bool = True) -> CaseOut:
    recordings = bundle.exhibits_for(session, case)
    exhibits = [_exhibit(session, r, i) for i, r in enumerate(recordings)] if with_exhibits else []

    audio_s = sum(float(r.duration_s or 0) for r in recordings)
    indexed_s = sum(e.indexed_s for e in exhibits) if exhibits else 0.0
    segments = sum(e.segment_count for e in exhibits) if exhibits else 0
    verified = (
        sum(e.review_counts.get(ReviewStatus.VERIFIED, 0) for e in exhibits) if exhibits else 0
    )
    cue_pairs = _cue_counts(session, [r.id for r in recordings]).values()
    cue_total = sum(pair[0] for pair in cue_pairs)
    cue_open = sum(pair[1] for pair in cue_pairs)

    return CaseOut(
        id=case.id,
        number=case.number,
        subject=case.subject,
        description=case.description,
        disclosure_due=case.disclosure_due,
        days_to_disclosure=_days_to(case.disclosure_due),
        owner=case.owner,
        status=case.status,
        exhibits=exhibits,
        totals={
            "exhibits": len(recordings),
            "audio_hours": round(audio_s / 3600, 2),
            "indexed_hours": round(indexed_s / 3600, 2),
            "segments": segments,
            "verified": verified,
            "reviewed_pct": round(verified / segments * 100, 1) if segments else 0.0,
            "cue_total": cue_total,
            "cue_open": cue_open,
        },
    )


@router.get("/cases", response_model=list[CaseOut])
def list_cases(
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> list[CaseOut]:
    cases = list(
        session.execute(select(Case).order_by(Case.disclosure_due.is_(None), Case.disclosure_due))
        .scalars()
    )
    return [_case_out(session, case) for case in cases]


@router.get("/cases/{case_id}", response_model=CaseOut)
def get_case(
    case_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> CaseOut:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return _case_out(session, case)


def _exhibit_labels(session: Session, case: Case) -> dict[str, str]:
    return {
        r.id: f"E-{i:03d}"
        for i, r in enumerate(bundle.exhibits_for(session, case), start=1)
    }


@router.get("/cases/{case_id}/cues", response_model=list[CueOut])
def list_cues(
    case_id: str,
    decision: str | None = Query(default=None),
    recording_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> list[CueOut]:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    labels = _exhibit_labels(session, case)
    if not labels:
        return []

    stmt = select(CueCandidate).where(CueCandidate.recording_id.in_(list(labels)))
    if decision:
        stmt = stmt.where(CueCandidate.decision == decision)
    if recording_id:
        stmt = stmt.where(CueCandidate.recording_id == recording_id)
    rows = list(session.execute(stmt.order_by(CueCandidate.start_s)).scalars())

    filenames = {
        r.id: r.filename
        for r in session.execute(
            select(Recording).where(Recording.id.in_(list(labels)))
        ).scalars()
    }
    return [
        CueOut(
            id=row.id,
            recording_id=row.recording_id,
            segment_id=row.segment_id,
            start_s=row.start_s,
            end_s=row.end_s,
            span_text=row.span_text,
            entity_type=row.entity_type,
            reason_code=row.reason_code,
            reason_label=cues.REASON_LABELS.get(row.reason_code, row.reason_code),
            confidence=round(row.confidence, 2),
            detector=row.detector,
            decision=row.decision,
            reviewer=row.reviewer,
            decided_at=row.decided_at,
            exhibit=labels.get(row.recording_id),
            filename=filenames.get(row.recording_id),
        )
        for row in rows
    ]


@router.get("/cases/{case_id}/cues.csv")
def export_cues(
    case_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("export")),
) -> Response:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    payload = bundle.cue_csv(session, case)
    audit.record(
        session,
        actor,
        "case.cues_exported",
        entity_type="case",
        entity_id=case.id,
        detail={"format": "csv"},
    )
    session.commit()
    return Response(
        content=payload,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{case.number}-cue-sheet.csv"'
        },
    )


@router.post("/cues/{cue_id}/decision", response_model=CueOut)
def decide_cue(
    cue_id: str,
    payload: CueDecisionIn,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("edit")),
) -> CueOut:
    cue = session.get(CueCandidate, cue_id)
    if cue is None:
        raise HTTPException(status_code=404, detail="Cue not found")

    cue.decision = payload.decision
    cue.reviewer = actor.name if payload.decision != CueDecision.OPEN else None
    cue.decided_at = datetime.now(timezone.utc) if payload.decision != CueDecision.OPEN else None

    audit.record(
        session,
        actor,
        f"cue.{payload.decision}",
        entity_type="cue",
        entity_id=cue.id,
        recording_id=cue.recording_id,
        detail={
            "span_text": cue.span_text,
            "reason_code": cue.reason_code,
            "start_s": cue.start_s,
        },
    )
    session.commit()

    return CueOut(
        id=cue.id,
        recording_id=cue.recording_id,
        segment_id=cue.segment_id,
        start_s=cue.start_s,
        end_s=cue.end_s,
        span_text=cue.span_text,
        entity_type=cue.entity_type,
        reason_code=cue.reason_code,
        reason_label=cues.REASON_LABELS.get(cue.reason_code, cue.reason_code),
        confidence=round(cue.confidence, 2),
        detector=cue.detector,
        decision=cue.decision,
        reviewer=cue.reviewer,
        decided_at=cue.decided_at,
    )


@router.post("/recordings/{recording_id}/cues/rescan")
def rescan_cues(
    recording_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("edit")),
) -> dict[str, int]:
    recording = session.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    created = cues.scan_recording(session, recording)
    audit.record(
        session,
        actor,
        "recording.cues_scanned",
        entity_type="recording",
        entity_id=recording.id,
        recording_id=recording.id,
        detail={"created": created},
    )
    session.commit()
    total = session.execute(
        select(func.count(CueCandidate.id)).where(
            CueCandidate.recording_id == recording_id
        )
    ).scalar_one()
    return {"created": created, "total": int(total)}


@router.get("/cases/{case_id}/cost", response_model=CostOut)
def case_cost(
    case_id: str,
    archive_hours: float = Query(default=2900.0, ge=0),
    tapes: int = Query(default=3900, ge=0),
    rate: float = Query(default=RATE_PER_HOUR, ge=0),
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> CostOut:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    recordings = list(
        session.execute(select(Recording).where(Recording.case_id == case.id)).scalars()
    )
    audio_hours = sum(float(r.duration_s or 0) for r in recordings) / 3600
    exhibit_count = len(recordings)

    def scenarios(hours: float, exhibits: int) -> list[CostScenarioOut]:
        full_compute = hours * RTF
        window_hours = min(hours, exhibits * WINDOW_SECONDS / 3600)
        window_compute = window_hours * RTF
        spotting_compute = hours * SPOTTING_RTF + hours * SPOTTING_WINNER_SHARE * RTF
        return [
            CostScenarioOut(
                key="full",
                label="Full index everything",
                detail="transcribe every recording once",
                compute_hours=round(full_compute, 2),
                cost=round(hours * rate, 2),
                coverage_pct=100.0,
                time_to_answer="minutes, forever",
            ),
            CostScenarioOut(
                key="window",
                label="Windowed promotion",
                detail=f"{int(WINDOW_SECONDS // 60)} min per recording, on request",
                compute_hours=round(window_compute, 2),
                cost=round(window_hours * rate, 2),
                coverage_pct=round(window_hours / hours * 100, 1) if hours else 0.0,
                time_to_answer="one recording at a time",
            ),
            CostScenarioOut(
                key="spotting",
                label="Spotting pass, then ASR",
                detail="cheap scan, full ASR on the winners",
                compute_hours=round(spotting_compute, 2),
                cost=round((hours * SPOTTING_RTF + hours * SPOTTING_WINNER_SHARE) * rate, 2),
                coverage_pct=round(SPOTTING_WINNER_SHARE * 100, 1),
                time_to_answer="hours, archive-wide",
            ),
        ]

    return CostOut(
        audio_hours=round(audio_hours, 2),
        exhibits=exhibit_count,
        rate_per_hour=rate,
        rtf=RTF,
        scenarios=scenarios(audio_hours, exhibit_count),
        archive_hours=round(archive_hours, 0),
        archive_tapes=tapes,
        archive_scenarios=scenarios(archive_hours, tapes),
        assumptions=[
            f"List price of ${rate:.2f} per audio hour of ASR compute.",
            f"Measured real-time factor {RTF} (small model, CPU int8) on this machine.",
            "Windowed promotion indexes a 4-minute window per recording, not the whole file.",
            f"Spotting pass runs at RTF {SPOTTING_RTF}, then full ASR on {int(SPOTTING_WINNER_SHARE * 100)}% of the archive.",
            "Costs exclude storage, review labour and the redaction pass itself.",
        ],
    )


@router.get("/cases/{case_id}/bundle", response_model=BundlePlanOut)
def bundle_plan(
    case_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("read")),
) -> BundlePlanOut:
    """What the bundle will contain, sized from the real payloads.

    The cover sheet is rendered here so its size is measured, not estimated: a
    manifest that lies about a file's size is worse than no manifest.
    """
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    exhibits = bundle.exhibits_for(session, case)
    states = {r.id: bundle.review_state(session, r) for r in exhibits}
    cue_counts = _cue_counts(session, [r.id for r in exhibits])

    unverified = [
        f"E-{index:03d}"
        for index, recording in enumerate(exhibits, start=1)
        if states[recording.id][0] == 0 or states[recording.id][1] < states[recording.id][0]
    ]
    warnings = (
        [
            f"{len(unverified)} exhibit(s) are not fully verified "
            f"({', '.join(unverified)}). The bundle can be built; the cover sheet records it."
        ]
        if unverified
        else []
    )

    sized: list[tuple[str, int]] = []
    for index, recording in enumerate(exhibits, start=1):
        source = Path(recording.original_path)
        if source.exists():
            sized.append((f"audio/E-{index:03d}_{source.name}", source.stat().st_size))
        sized.append(
            (f"transcripts/E-{index:03d}.json", len(bundle._transcript_payload(session, recording)))
        )
        sized.append(
            (f"custody/E-{index:03d}.json", len(bundle._custody_payload(session, recording)))
        )
    sized.append(("cue_sheet.csv", len(bundle.cue_csv(session, case))))

    # Hash length is fixed, so rendering with a placeholder hash gives the exact
    # PDF size the real bundle will carry.
    cover = bundle.cover_sheet_pdf(
        case, exhibits, actor.name, states, cue_counts, "0" * 64, warnings
    )
    sized.append(("cover_sheet.pdf", len(cover)))
    manifest_bytes = sum(len(f"{'0' * 64}  {name}\n") for name, _ in sized)
    sized.append(("manifest.sha256", manifest_bytes))

    items = [
        BundleItemOut(name=name, bytes=size, kind=name.split("/")[0])
        for name, size in sized
    ]
    return BundlePlanOut(
        case_id=case.id,
        number=case.number,
        items=items,
        total_bytes=sum(size for _, size in sized),
        warnings=warnings,
        unverified_exhibits=unverified,
    )


@router.get("/cases/{case_id}/bundle.zip")
def bundle_download(
    case_id: str,
    session: Session = Depends(get_session),
    actor: Actor = Depends(require("export")),
) -> FileResponse:
    case = session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    tmp = Path(tempfile.mkdtemp(prefix="bundle-")) / f"{case.number}.zip"
    result = bundle.build_zip(session, case, tmp, reviewer=actor.name)
    audit.record(
        session,
        actor,
        "case.bundle_built",
        entity_type="case",
        entity_id=case.id,
        detail={
            "files": len(result["items"]),
            "manifest_sha256": result["manifest_sha256"],
            "warnings": result["warnings"],
        },
    )
    session.commit()
    return FileResponse(
        tmp,
        media_type="application/zip",
        filename=f"{case.number}-disclosure.zip",
        headers={"X-Manifest-Sha256": result["manifest_sha256"]},
    )
