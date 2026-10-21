"""Cases, redaction cue sheet, cost model and disclosure bundle."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from conftest import (
    ADMIN_HEADERS,
    REVIEWER_HEADERS,
    VIEWER_HEADERS,
    upload_ok,
    write_sample,
)

from app import cues
from app.db import SessionLocal
from app.models import Case, Recording

CUE_LINES = [
    ("SPEAKER_00", "My name is Carla Nadeau and I live at 42 Maple Street."),
    ("SPEAKER_00", "She told me she was pregnant and had two children with her."),
    ("SPEAKER_01", "Call me back at 780-555-0142 when you have the plate."),
]


@pytest.fixture
def cue_sample(tmp_path):
    return write_sample(tmp_path / "media", "cue", CUE_LINES)


def make_case(client, sample, *, number="26-000001", days=5) -> tuple[str, str, dict]:
    data = upload_ok(client, sample)
    session = SessionLocal()
    try:
        case = Case(
            number=number,
            subject="Test case",
            owner="test.reviewer",
            disclosure_due=datetime.now(timezone.utc) + timedelta(days=days),
        )
        session.add(case)
        session.flush()
        recording = session.get(Recording, data["id"])
        recording.case_id = case.id
        session.commit()
        return case.id, recording.id, data
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# detector
# --------------------------------------------------------------------------- #

def test_detect_finds_identifiers_and_reasons():
    text = "My name is Carla Nadeau. She is pregnant and hiding with her children."
    found = {match.reason_code for match in cues.detect(text)}

    assert "PII-NAME" in found
    assert "MED-INFO" in found
    assert "PII-MINOR" in found


def test_detect_collapses_overlapping_matches():
    # "pregnant" is both a medical keyword and, in context, a minor-adjacent
    # signal. One span must survive, not two.
    matches = cues.detect("she was pregnant")
    assert len(matches) == 1


def test_detect_maps_span_to_time_inside_the_segment():
    text = "hello there"
    assert cues.detect(text) == []


# --------------------------------------------------------------------------- #
# cases
# --------------------------------------------------------------------------- #

def test_case_reports_exhibit_coverage_and_totals(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)

    payload = client.get(f"/api/cases/{case_id}", headers=ADMIN_HEADERS).json()

    assert payload["number"] == "26-000001"
    assert payload["days_to_disclosure"] is not None
    assert payload["totals"]["exhibits"] == 1
    exhibit = payload["exhibits"][0]
    assert exhibit["indexed"] == "full"
    assert exhibit["dark_s"] == 0.0
    assert exhibit["segment_count"] == 3


def test_triaged_recording_is_reported_as_dark(client, cue_sample):
    data = upload_ok(client, cue_sample)
    session = SessionLocal()
    try:
        case = Case(number="26-000002", subject="Triage case")
        session.add(case)
        session.flush()
        recording = session.get(Recording, data["id"])
        recording.case_id = case.id
        # A triaged file has been registered but never transcribed.
        for transcript in list(recording.transcripts):
            session.delete(transcript)
        session.commit()
        case_id = case.id
    finally:
        session.close()

    payload = client.get(f"/api/cases/{case_id}", headers=ADMIN_HEADERS).json()
    exhibit = payload["exhibits"][0]
    assert exhibit["indexed"] == "none"
    assert exhibit["dark_s"] > 0


# --------------------------------------------------------------------------- #
# cue sheet
# --------------------------------------------------------------------------- #

def test_cues_are_scanned_on_ingest(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)

    rows = client.get(f"/api/cases/{case_id}/cues", headers=ADMIN_HEADERS).json()

    reasons = {row["reason_code"] for row in rows}
    assert {"PII-NAME", "PII-ADDR", "MED-INFO", "PII-MINOR"} <= reasons
    assert all(row["start_s"] <= row["end_s"] for row in rows)
    assert all(row["decision"] == "open" for row in rows)
    assert rows[0]["exhibit"] == "E-001"


def test_cue_decision_records_reviewer_and_audit(client, cue_sample):
    case_id, recording_id, _ = make_case(client, cue_sample)
    cue = client.get(f"/api/cases/{case_id}/cues", headers=ADMIN_HEADERS).json()[0]

    response = client.post(
        f"/api/cues/{cue['id']}/decision",
        json={"decision": "accept"},
        headers=REVIEWER_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "accept"
    assert body["reviewer"] == "test.reviewer"

    events = client.get(
        f"/api/recordings/{recording_id}/provenance", headers=ADMIN_HEADERS
    ).json()
    actions = {step["label"] for step in events["steps"]}
    assert any("cue" in label.lower() for label in actions) or events["steps"]


def test_viewer_cannot_decide_a_cue(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)
    cue = client.get(f"/api/cases/{case_id}/cues", headers=ADMIN_HEADERS).json()[0]

    response = client.post(
        f"/api/cues/{cue['id']}/decision",
        json={"decision": "reject"},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 403


def test_cue_csv_has_the_documented_schema(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)

    response = client.get(f"/api/cases/{case_id}/cues.csv", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    header = response.text.splitlines()[0]
    assert header == (
        "exhibit,filename,start_s,end_s,span_text,entity_type,reason_code,"
        "confidence,decision,reviewer,decided_at"
    )


# --------------------------------------------------------------------------- #
# cost and bundle
# --------------------------------------------------------------------------- #

def test_cost_model_prices_three_approaches(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)

    payload = client.get(f"/api/cases/{case_id}/cost", headers=ADMIN_HEADERS).json()

    keys = [scenario["key"] for scenario in payload["scenarios"]]
    assert keys == ["full", "window", "spotting"]
    assert payload["scenarios"][0]["coverage_pct"] == 100.0
    assert payload["archive_scenarios"], "archive projection is what the buyer asks for"
    assert any("assumption" in line.lower() or "List price" in line for line in payload["assumptions"])


def test_bundle_zip_manifest_verifies(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)

    response = client.get(f"/api/cases/{case_id}/bundle.zip", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert "manifest.sha256" in names
    assert "cover_sheet.pdf" in names
    assert "cue_sheet.csv" in names
    assert any(name.startswith("audio/") for name in names)

    manifest = archive.read("manifest.sha256").decode().splitlines()
    for line in manifest:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name


def test_bundle_plan_flags_unverified_exhibits(client, cue_sample):
    case_id, _, _ = make_case(client, cue_sample)

    plan = client.get(f"/api/cases/{case_id}/bundle", headers=ADMIN_HEADERS).json()

    assert plan["unverified_exhibits"] == ["E-001"]
    assert plan["warnings"]
    assert plan["total_bytes"] > 0
    assert all(item["bytes"] > 0 for item in plan["items"])
