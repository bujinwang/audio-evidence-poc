"""Disclosure bundle: the artifact that actually leaves the building.

Contents, per the design brief:

    audio/<exhibit>_<filename>      the original bytes, unmodified
    transcripts/<exhibit>.json      machine text, current text, revisions
    custody/<exhibit>.json          audit events for that exhibit
    cue_sheet.csv                   redaction worksheet, one row per span
    cover_sheet.pdf                 case, exhibits, preparer, review state, hash
    manifest.sha256                 sha256 of every file above

The manifest is computed over the files as written, so `sha256sum -c` verifies the
package offline. Nothing here rewrites an original.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AuditEvent,
    Case,
    CueCandidate,
    Recording,
    Revision,
    Segment,
    Transcript,
    TranscriptVariant,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def exhibits_for(session: Session, case: Case) -> list[Recording]:
    """Exhibits in exhibit order: recording date first, then intake date.

    Exhibit numbers must be stable across sessions and must match the paper file,
    so they follow the recording date rather than the order someone uploaded.
    """
    return list(
        session.execute(
            select(Recording)
            .where(Recording.case_id == case.id)
            .order_by(
                Recording.recorded_at.asc().nullslast(),
                Recording.created_at,
            )
        ).scalars()
    )


def review_state(session: Session, recording: Recording) -> tuple[int, int]:
    """(segments, verified) for the original transcript."""
    transcript = session.execute(
        select(Transcript)
        .where(
            Transcript.recording_id == recording.id,
            Transcript.variant == TranscriptVariant.ORIGINAL,
        )
        .limit(1)
    ).scalar_one_or_none()
    if transcript is None:
        return 0, 0
    rows = session.execute(
        select(Segment.review_status).where(Segment.transcript_id == transcript.id)
    ).scalars()
    total = verified = 0
    for status_value in rows:
        total += 1
        if status_value == "verified":
            verified += 1
    return total, verified


def _transcript_payload(session: Session, recording: Recording) -> bytes:
    transcripts = list(
        session.execute(
            select(Transcript).where(Transcript.recording_id == recording.id)
        ).scalars()
    )
    payload = []
    for transcript in transcripts:
        segments = list(
            session.execute(
                select(Segment)
                .where(Segment.transcript_id == transcript.id)
                .order_by(Segment.idx)
            ).scalars()
        )
        revisions = list(
            session.execute(
                select(Revision)
                .where(Revision.segment_id.in_([s.id for s in segments] or [""]))
                .order_by(Revision.created_at)
            ).scalars()
        )
        payload.append(
            {
                "variant": transcript.variant,
                "engine": transcript.engine,
                "model_version": transcript.model_version,
                "is_reference_copy": transcript.is_reference_copy,
                "segments": [
                    {
                        "idx": s.idx,
                        "start_s": s.start_s,
                        "end_s": s.end_s,
                        "speaker": s.speaker,
                        "text": s.text,
                        "ai_text": s.ai_text,
                        "review_status": s.review_status,
                        "version": s.version,
                        "verified_by": s.verified_by,
                        "verified_at": s.verified_at.isoformat() if s.verified_at else None,
                    }
                    for s in segments
                ],
                "revisions": [
                    {
                        "segment_idx": next(
                            (s.idx for s in segments if s.id == r.segment_id), None
                        ),
                        "version": r.version,
                        "prev_text": r.prev_text,
                        "new_text": r.new_text,
                        "actor": r.actor,
                        "note": r.note,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in revisions
                ],
            }
        )
    return json.dumps(
        {
            "recording_id": recording.id,
            "filename": recording.filename,
            "sha256": recording.sha256,
            "transcripts": payload,
        },
        indent=2,
        default=str,
    ).encode()


def _custody_payload(session: Session, recording: Recording) -> bytes:
    events = list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.recording_id == recording.id)
            .order_by(AuditEvent.ts)
        ).scalars()
    )
    return json.dumps(
        {
            "recording_id": recording.id,
            "filename": recording.filename,
            "sha256": recording.sha256,
            "original_path": recording.original_path,
            "uploaded_by": recording.uploaded_by,
            "recorded_at": recording.recorded_at.isoformat() if recording.recorded_at else None,
            "events": [
                {
                    "ts": e.ts.isoformat(),
                    "actor": e.actor,
                    "role": e.role,
                    "action": e.action,
                    "entity_type": e.entity_type,
                    "entity_id": e.entity_id,
                    "detail": e.detail,
                }
                for e in events
            ],
        },
        indent=2,
        default=str,
    ).encode()


def cue_rows(session: Session, case: Case) -> list[dict]:
    exhibits = exhibits_for(session, case)
    order = {rec.id: index + 1 for index, rec in enumerate(exhibits)}
    rows = []
    for recording in exhibits:
        cues = list(
            session.execute(
                select(CueCandidate)
                .where(CueCandidate.recording_id == recording.id)
                .order_by(CueCandidate.start_s)
            ).scalars()
        )
        for cue in cues:
            rows.append(
                {
                    "exhibit": f"E-{order[recording.id]:03d}",
                    "filename": recording.filename,
                    "start_s": f"{cue.start_s:.2f}",
                    "end_s": f"{cue.end_s:.2f}",
                    "span_text": cue.span_text,
                    "entity_type": cue.entity_type,
                    "reason_code": cue.reason_code,
                    "confidence": f"{cue.confidence:.2f}",
                    "decision": cue.decision,
                    "reviewer": cue.reviewer or "",
                    "decided_at": cue.decided_at.isoformat() if cue.decided_at else "",
                }
            )
    return rows


def cue_csv(session: Session, case: Case) -> bytes:
    rows = cue_rows(session, case)
    fields = [
        "exhibit",
        "filename",
        "start_s",
        "end_s",
        "span_text",
        "entity_type",
        "reason_code",
        "confidence",
        "decision",
        "reviewer",
        "decided_at",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def cover_sheet_pdf(
    case: Case,
    exhibits: list[Recording],
    reviewer: str,
    review_states: dict[str, tuple[int, int]],
    cue_counts: dict[str, tuple[int, int]],
    manifest_sha: str,
    warnings: list[str],
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title=f"Disclosure package {case.number}",
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    label = ParagraphStyle(
        "label", parent=small, fontSize=7, textColor=colors.HexColor("#666e79"), spaceAfter=1
    )
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9, leading=13)
    title = ParagraphStyle(
        "title", parent=styles["Title"], fontSize=17, alignment=0, spaceAfter=2
    )

    story = [
        Paragraph("TECHNOLOGY NORTH · EVIDENCE SERVICES", label),
        Spacer(1, 6),
        Paragraph("Disclosure package", title),
        Spacer(1, 12),
    ]

    header_rows = [
        ["Case", case.number],
        ["Subject", case.subject],
        ["Prepared by", reviewer],
        ["Prepared", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
        [
            "Disclosure due",
            case.disclosure_due.strftime("%Y-%m-%d") if case.disclosure_due else "not set",
        ],
        ["Manifest", f"sha256:{manifest_sha[:32]}…" if manifest_sha else "pending"],
    ]
    table = Table(header_rows, colWidths=[1.3 * inch, 5.1 * inch])
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (0, -1), "Helvetica", 8),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#666e79")),
                ("FONT", (1, 0), (1, -1), "Helvetica", 9),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#e1ded6")),
            ]
        )
    )
    story += [table, Spacer(1, 16), Paragraph("Exhibits", label), Spacer(1, 4)]

    rows = [["Exhibit", "Recording", "Length", "Reviewed", "Cues", "State"]]
    for index, recording in enumerate(exhibits, start=1):
        total, verified = review_states.get(recording.id, (0, 0))
        cue_total, cue_open = cue_counts.get(recording.id, (0, 0))
        minutes, seconds = divmod(int(recording.duration_s or 0), 60)
        rows.append(
            [
                f"E-{index:03d}",
                recording.filename[:46],
                f"{minutes}:{seconds:02d}",
                f"{verified}/{total}" if total else "—",
                f"{cue_total}" + (f" ({cue_open} open)" if cue_open else ""),
                "verified" if total and verified == total else ("partial" if verified else "unreviewed"),
            ]
        )
    exhibit_table = Table(
        rows, colWidths=[0.6 * inch, 2.9 * inch, 0.6 * inch, 0.8 * inch, 0.9 * inch, 0.9 * inch]
    )
    exhibit_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#666e79")),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cac6bb")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#e1ded6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story += [exhibit_table, Spacer(1, 14)]

    if warnings:
        story += [
            Paragraph("Review state at time of preparation", label),
            Spacer(1, 2),
            Paragraph(" ".join(warnings), body),
            Spacer(1, 12),
        ]

    story += [
        Paragraph(
            "Machine transcripts are identified per segment and are not evidence of truth. "
            "Corrections and verifications are recorded with reviewer identity and time. "
            "Redactions are listed in <i>cue_sheet.csv</i> with reason codes; no audio in this "
            "package has been altered. Verify the contents with <font face='Courier'>sha256sum -c "
            "manifest.sha256</font>.",
            small,
        ),
        Spacer(1, 22),
        Paragraph("Prepared by ______________________________    Date ________________", small),
    ]

    doc.build(story)
    return buffer.getvalue()


def build_zip(session: Session, case: Case, dest: Path, reviewer: str) -> dict:
    exhibits = exhibits_for(session, case)
    review_states = {r.id: review_state(session, r) for r in exhibits}
    cue_counts: dict[str, tuple[int, int]] = {}
    for recording in exhibits:
        cues = list(
            session.execute(
                select(CueCandidate).where(CueCandidate.recording_id == recording.id)
            ).scalars()
        )
        cue_counts[recording.id] = (
            len(cues),
            sum(1 for c in cues if c.decision == "open"),
        )

    unverified = [
        f"E-{index:03d}"
        for index, recording in enumerate(exhibits, start=1)
        if review_states[recording.id][0] == 0
        or review_states[recording.id][1] < review_states[recording.id][0]
    ]
    warnings = []
    if unverified:
        warnings.append(
            f"{len(unverified)} exhibit(s) are not fully verified ({', '.join(unverified)}). "
            "The bundle can be built; this sheet records the state at preparation."
        )

    entries: list[tuple[str, bytes]] = []
    for index, recording in enumerate(exhibits, start=1):
        source = Path(recording.original_path)
        if source.exists():
            entries.append(
                (f"audio/E-{index:03d}_{source.name}", source.read_bytes())
            )
        entries.append((f"transcripts/E-{index:03d}.json", _transcript_payload(session, recording)))
        entries.append((f"custody/E-{index:03d}.json", _custody_payload(session, recording)))

    entries.append(("cue_sheet.csv", cue_csv(session, case)))

    provisional = {name: _sha256(data) for name, data in entries}
    manifest_sha = _sha256("".join(provisional.values()).encode())
    entries.append(
        (
            "cover_sheet.pdf",
            cover_sheet_pdf(
                case, exhibits, reviewer, review_states, cue_counts, manifest_sha, warnings
            ),
        )
    )

    manifest_lines = [f"{_sha256(data)}  {name}" for name, data in entries]
    manifest = ("\n".join(manifest_lines) + "\n").encode()

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
        archive.writestr("manifest.sha256", manifest)

    return {
        "items": [{"name": n, "bytes": len(d), "kind": n.split("/")[0]} for n, d in entries]
        + [{"name": "manifest.sha256", "bytes": len(manifest), "kind": "manifest"}],
        "manifest_sha256": manifest_sha,
        "warnings": warnings,
        "unverified": unverified,
    }
