"""Redaction cue detection.

The application does not blur audio. It marks spans a redaction operator should
look at, with a reason code that survives disclosure, and exports the worksheet.

This is deliberately a **rule engine**, not a model: regex for structured
identifiers (phones, addresses, dates, plates), keyword lists for the categories a
redaction standard actually names (minors, medical, third-party address), and a
small honorific+name pattern. Every candidate carries the detector name and a
confidence, so a reviewer can see how it was found. A learned NER pass can replace
`_rules()` later without changing the schema or the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import CueCandidate, CueDecision, Recording, Segment, Transcript, TranscriptVariant

_WORD = re.compile(r"\S+")

# Structured identifiers. Confidence reflects how reliable the pattern is, not how
# sensitive the span is.
_STRUCTURED: list[tuple[re.Pattern[str], str, str, float]] = [
    (
        re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        "Phone",
        "PII-PHONE",
        0.90,
    ),
    (
        re.compile(
            r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){0,3}"
            r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|"
            r"Way|Court|Ct|Place|Pl)\b"
        ),
        "Address",
        "PII-ADDR",
        0.85,
    ),
    (
        re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b"),
        "Date",
        "PII-DOB",
        0.80,
    ),
    (
        # Ontario/Alberta style plate: three letters, three or four digits.
        re.compile(r"\b[A-Z]{3}[-\s]?\d{3,4}\b"),
        "Plate",
        "PII-PLATE",
        0.55,
    ),
]

# Categories a redaction standard names in words rather than formats.
_KEYWORDS: list[tuple[re.Pattern[str], str, str, float]] = [
    (
        re.compile(
            r"\b(?:kid|kids|child|children|son|daughter|stepson|stepdaughter|"
            r"minor|toddler|infant|baby)\b",
            re.I,
        ),
        "Minor",
        "PII-MINOR",
        0.65,
    ),
    (
        re.compile(
            r"\b(?:pregnant|injur\w*|hospital|medicat\w*|diagnos\w*|overdose|"
            r"wound\w*|surgery|therapy|ambulance)\b",
            re.I,
        ),
        "Medical",
        "MED-INFO",
        0.60,
    ),
    (
        # Capture the name itself, not just the phrase that introduces it.
        re.compile(r"\b(?:my name is|name's)\s+[A-Z][a-z]+", re.I),
        "Person",
        "PII-NAME",
        0.70,
    ),
    (
        re.compile(r"\b(?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z][a-z]+", re.I),
        "Person",
        "PII-NAME",
        0.55,
    ),
    (
        re.compile(r"\b(?:address|apartment|unit\s+\d+|postal code)\b", re.I),
        "Address",
        "PII-ADDR",
        0.45,
    ),
]

# Reason codes are shown verbatim in the UI and in the export. Keep them short and
# stable; the customer's own taxonomy replaces this list, it does not extend it.
REASON_LABELS = {
    "PII-PHONE": "Phone number",
    "PII-ADDR": "Address",
    "PII-DOB": "Date of birth",
    "PII-PLATE": "Licence plate",
    "PII-MINOR": "Minor",
    "PII-NAME": "Person name",
    "MED-INFO": "Medical information",
}


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    text: str
    entity_type: str
    reason_code: str
    confidence: float


def _rules() -> list[tuple[re.Pattern[str], str, str, float]]:
    return _STRUCTURED + _KEYWORDS


def detect(text: str) -> list[Match]:
    """Pure function: spans in `text` that a redaction pass should look at.

    Overlapping matches are collapsed, keeping the highest-confidence one, so a
    "three months pregnant" hit is not reported twice by two keyword rules.
    """
    if not text:
        return []
    found: list[Match] = []
    for pattern, entity, reason, confidence in _rules():
        for match in pattern.finditer(text):
            span = match.group(0).strip()
            if not span:
                continue
            found.append(
                Match(
                    start=match.start(),
                    end=match.start() + len(match.group(0)),
                    text=span,
                    entity_type=entity,
                    reason_code=reason,
                    confidence=confidence,
                )
            )

    found.sort(key=lambda m: (m.start, -m.confidence, -(m.end - m.start)))
    kept: list[Match] = []
    for candidate in found:
        if any(candidate.start < k.end and candidate.end > k.start for k in kept):
            continue
        kept.append(candidate)
    return kept


def _word_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _WORD.finditer(text)]


def _time_at(segment: Segment, char_index: int, *, end: bool) -> float:
    """Map a character offset to a time, using word timings when they align."""
    text = segment.text or ""
    spans = _word_spans(text)
    words = segment.words or []

    if words and len(words) == len(spans):
        index = None
        for position, (start, stop) in enumerate(spans):
            # A match's end offset is exclusive: it belongs to the word it closes,
            # not to the next word. Getting this wrong produced cues whose end
            # preceded their start.
            if start <= char_index < stop or (end and char_index == stop):
                index = position
                break
        if index is not None:
            word = words[index]
            return float(word.get("end" if end else "start", segment.start_s))
    ratio = min(max(char_index / max(len(text), 1), 0.0), 1.0)
    return segment.start_s + (segment.end_s - segment.start_s) * ratio


def scan_recording(session: Session, recording: Recording) -> int:
    """(Re)build the cue candidates for one recording.

    Decisions already made by a reviewer are preserved: the scan is a detector,
    not a reset. A candidate is re-created only if no decided row covers the same
    (segment, span, reason) triple.
    """
    transcript = session.execute(
        select(Transcript)
        .where(
            Transcript.recording_id == recording.id,
            Transcript.variant == TranscriptVariant.ORIGINAL,
        )
        .limit(1)
    ).scalar_one_or_none()
    if transcript is None:
        return 0

    decided = {
        (row.segment_id, row.span_text, row.reason_code)
        for row in session.execute(
            select(CueCandidate).where(
                CueCandidate.recording_id == recording.id,
                CueCandidate.decision != CueDecision.OPEN,
            )
        ).scalars()
    }

    session.execute(
        delete(CueCandidate).where(
            CueCandidate.recording_id == recording.id,
            CueCandidate.decision == CueDecision.OPEN,
        )
    )

    segments = list(
        session.execute(
            select(Segment).where(Segment.transcript_id == transcript.id).order_by(Segment.idx)
        ).scalars()
    )

    created = 0
    for segment in segments:
        for match in detect(segment.text or ""):
            if (segment.id, match.text, match.reason_code) in decided:
                continue
            session.add(
                CueCandidate(
                    recording_id=recording.id,
                    segment_id=segment.id,
                    start_s=round(_time_at(segment, match.start, end=False), 3),
                    end_s=round(_time_at(segment, match.end, end=True), 3),
                    span_text=match.text,
                    entity_type=match.entity_type,
                    reason_code=match.reason_code,
                    confidence=match.confidence,
                    detector="rules",
                )
            )
            created += 1

    session.flush()
    return created
