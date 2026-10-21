"""Chain-of-custody: concurrent edits must never share a revision version.

Two reviewers correcting the same passage at the same moment used to each read
`Segment.version`, each compute `version + 1`, and each insert a `Revision` with
the same number. The audit trail then claimed two different corrections were
both "version 2", which makes the correction history for that passage
ambiguous — the defect this module guards against.

The requests are issued from several threads against the same segment, which is
the same shape as two browser tabs or a client retrying after a timeout. No UI
is involved: the invariant has to hold at the API boundary.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from conftest import REVIEWER_HEADERS, upload_ok

CONCURRENT_EDITS = 5


def first_segment(client, recording_id: str) -> dict:
    transcript = client.get(f"/api/recordings/{recording_id}/transcript").json()
    return transcript["segments"][0]


def segment_state(client, recording_id: str) -> dict:
    transcript = client.get(f"/api/recordings/{recording_id}/transcript").json()
    return transcript["segments"][0]


def test_concurrent_edits_allocate_distinct_revision_versions(client, witness_sample):
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])
    assert segment["version"] == 1, "machine original starts at version 1"

    def edit(index: int):
        return client.patch(
            f"/api/segments/{segment['id']}",
            json={"text": f"Concurrent correction number {index}.", "note": f"tab {index}"},
            headers=REVIEWER_HEADERS,
        )

    with ThreadPoolExecutor(max_workers=CONCURRENT_EDITS) as pool:
        responses = list(pool.map(edit, range(CONCURRENT_EDITS)))

    # Every correction must be accepted: a unique index is not allowed to turn
    # a legitimate concurrent edit into a 500.
    for response in responses:
        assert response.status_code == 200, response.text

    history = client.get(f"/api/segments/{segment['id']}/history").json()
    versions = [revision["version"] for revision in history["revisions"]]

    assert len(versions) == CONCURRENT_EDITS, (
        f"expected one revision per edit, got {versions}"
    )
    assert len(set(versions)) == CONCURRENT_EDITS, (
        f"duplicate revision versions in the audit trail: {versions}"
    )
    assert versions == sorted(versions), f"revision chain is out of order: {versions}"
    assert versions == list(range(2, 2 + CONCURRENT_EDITS)), (
        f"revision chain must be contiguous starting at 2, got {versions}"
    )

    # The segment must agree with its own revision chain: the surviving version
    # is the highest allocated one, not a value one of the racers computed.
    current = segment_state(client, recording["id"])
    assert current["version"] == max(versions)

    # Every reported version must be a version that actually exists in the chain.
    reported = []
    for response in responses:
        reported.append(response.json()["version"])
    assert sorted(reported) == versions

    # And the audit trail must carry the same numbers, so an auditor reading the
    # events and the revisions sees one consistent story.
    events = client.get(
        f"/api/audit?entity_id={segment['id']}", headers=REVIEWER_HEADERS
    ).json()
    edited_versions = sorted(
        event["detail"]["version"] for event in events if event["action"] == "segment.edited"
    )
    assert edited_versions == versions


def test_revision_chain_stays_contiguous_under_sequential_edits(client, witness_sample):
    """Baseline the guard must not break: one-at-a-time edits still number 2, 3, 4."""
    recording = upload_ok(client, witness_sample)
    segment = first_segment(client, recording["id"])

    for index in range(3):
        response = client.patch(
            f"/api/segments/{segment['id']}",
            json={"text": f"Sequential correction {index}."},
            headers=REVIEWER_HEADERS,
        )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == index + 2

    history = client.get(f"/api/segments/{segment['id']}/history").json()
    assert [revision["version"] for revision in history["revisions"]] == [2, 3, 4]
