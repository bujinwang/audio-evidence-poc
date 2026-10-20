"""FR-06 / FR-07: correction, explicit verification, revision history, audit trail."""

from __future__ import annotations

from conftest import ADMIN_HEADERS, REVIEWER_HEADERS, VIEWER_HEADERS, upload_ok


def first_segment(client, recording_id: str) -> dict:
    transcript = client.get(f"/api/recordings/{recording_id}/transcript").json()
    return transcript["segments"][0]


def test_edit_preserves_ai_text_and_records_a_revision(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])
    original_ai_text = segment["ai_text"]

    response = client.patch(
        f"/api/segments/{segment['id']}",
        json={"text": "This is Detective Morrow speaking with Carla Nadeau.", "note": "name spelling"},
        headers=REVIEWER_HEADERS,
    )
    assert response.status_code == 200, response.text
    updated = response.json()

    assert updated["text"] == "This is Detective Morrow speaking with Carla Nadeau."
    assert updated["ai_text"] == original_ai_text, "AI output must be immutable"
    assert updated["review_status"] == "corrected"
    assert updated["version"] == 2
    assert updated["edited"] is True

    history = client.get(f"/api/segments/{segment['id']}/history").json()
    assert len(history["revisions"]) == 1
    revision = history["revisions"][0]
    assert revision["prev_text"] == original_ai_text
    assert revision["new_text"] == updated["text"]
    assert revision["actor"] == "test.reviewer"
    assert revision["note"] == "name spelling"
    assert history["ai_text"] == original_ai_text


def test_correction_does_not_imply_verification(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.patch(
        f"/api/segments/{segment['id']}",
        json={"text": "Corrected text."},
        headers=REVIEWER_HEADERS,
    )
    current = client.get(f"/api/recordings/{recording['id']}/transcript").json()["segments"][0]
    assert current["review_status"] == "corrected"
    assert current["verified_by"] is None
    assert current["verified_at"] is None


def test_verification_is_explicit_and_attributed(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    response = client.post(
        f"/api/segments/{segment['id']}/verify",
        json={"verified": True, "note": "checked against audio"},
        headers=REVIEWER_HEADERS,
    )
    assert response.status_code == 200
    verified = response.json()

    assert verified["review_status"] == "verified"
    assert verified["verified_by"] == "test.reviewer"
    assert verified["verified_at"] is not None

    recording_state = client.get(f"/api/recordings/{recording['id']}").json()
    assert recording_state["review_counts"]["verified"] == 1
    assert recording_state["review_counts"]["ai_generated"] == 4


def test_editing_a_verified_segment_invalidates_the_verification(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.post(
        f"/api/segments/{segment['id']}/verify",
        json={"verified": True},
        headers=REVIEWER_HEADERS,
    )
    response = client.patch(
        f"/api/segments/{segment['id']}",
        json={"text": "Changed after verification."},
        headers=REVIEWER_HEADERS,
    )
    assert response.status_code == 200
    updated = response.json()

    assert updated["review_status"] == "corrected"
    assert updated["verified_by"] is None

    actions = [event["action"] for event in client.get(
        f"/api/audit?entity_id={segment['id']}", headers=ADMIN_HEADERS
    ).json()]
    assert "segment.verification_invalidated" in actions


def test_verification_can_be_withdrawn(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.post(f"/api/segments/{segment['id']}/verify", json={"verified": True},
                headers=REVIEWER_HEADERS)
    response = client.post(f"/api/segments/{segment['id']}/verify",
                           json={"verified": False, "note": "second listen"}, headers=REVIEWER_HEADERS)

    assert response.status_code == 200
    assert response.json()["review_status"] == "ai_generated"
    assert response.json()["verified_by"] is None


def test_reopen_moves_segment_back_into_review(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.post(f"/api/segments/{segment['id']}/verify", json={"verified": True},
                headers=REVIEWER_HEADERS)
    response = client.post(f"/api/segments/{segment['id']}/reopen", headers=REVIEWER_HEADERS)

    assert response.status_code == 200
    assert response.json()["review_status"] == "in_review"
    assert response.json()["verified_by"] is None


def test_empty_text_cannot_be_verified(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.patch(f"/api/segments/{segment['id']}", json={"text": "   "},
                 headers=REVIEWER_HEADERS)
    response = client.post(f"/api/segments/{segment['id']}/verify", json={"verified": True},
                           headers=REVIEWER_HEADERS)
    assert response.status_code == 400


def test_viewer_cannot_edit_or_verify(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    edit = client.patch(f"/api/segments/{segment['id']}", json={"text": "x"},
                        headers=VIEWER_HEADERS)
    assert edit.status_code == 403

    verify = client.post(f"/api/segments/{segment['id']}/verify", json={"verified": True},
                         headers=VIEWER_HEADERS)
    assert verify.status_code == 403


def test_viewer_cannot_read_audit_trail(client, witness_sample):
    upload_ok(client, witness_sample)
    response = client.get("/api/audit", headers=VIEWER_HEADERS)
    assert response.status_code == 403


def test_audit_trail_captures_the_review_lifecycle(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.patch(f"/api/segments/{segment['id']}", json={"text": "Reviewed text."},
                 headers=REVIEWER_HEADERS)
    client.post(f"/api/segments/{segment['id']}/verify", json={"verified": True},
                headers=REVIEWER_HEADERS)

    events = client.get(f"/api/audit?recording_id={recording['id']}", headers=ADMIN_HEADERS).json()
    actions = [event["action"] for event in events]

    assert "recording.uploaded" in actions
    assert "processing.completed" in actions
    assert "segment.edited" in actions
    assert "segment.verified" in actions
    assert all(event["actor"] for event in events)
    assert all(event["ts"] for event in events)

    upload_event = next(e for e in events if e["action"] == "recording.uploaded")
    assert upload_event["detail"]["sha256"] == recording["sha256"]


def test_audit_csv_export(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])
    client.patch(f"/api/segments/{segment['id']}", json={"text": "Export me."},
                 headers=REVIEWER_HEADERS)

    response = client.get("/api/audit/export.csv", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text
    assert "timestamp_utc,actor,role,action" in body
    assert "segment.edited" in body


def test_history_combines_revisions_and_audit_events(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    client.patch(f"/api/segments/{segment['id']}", json={"text": "First pass."},
                 headers=REVIEWER_HEADERS)
    client.patch(f"/api/segments/{segment['id']}", json={"text": "Second pass."},
                 headers=REVIEWER_HEADERS)

    history = client.get(f"/api/segments/{segment['id']}/history").json()
    assert [r["version"] for r in history["revisions"]] == [2, 3]
    assert history["revisions"][0]["prev_text"] == segment["ai_text"]
    assert history["revisions"][1]["prev_text"] == "First pass."
    assert len(history["events"]) >= 2
