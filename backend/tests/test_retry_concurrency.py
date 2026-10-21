"""Concurrency guard: one exhibit, at most one active processing job.

`POST /api/recordings/{id}/retry` used to read "is a job active?" and only then
insert one. Two callers both read "no" before either committed and both
inserted, because nothing in the schema stopped them:

    responses:  500  500  500  200
    jobs created: 4  →  1 succeeded, 3 stuck at status="running"

Two harms followed. Four transcription+embedding passes raced over the same
rows, which is expensive and can interleave writes to `transcripts`/`segments`/
`segment_embeddings`. And the three losers stayed `running` forever —
`run_job()` returns early for a job it finds in `running` — so `review_locked()`
and the active-job check then refused every later retry with 409. The exhibit
became impossible to re-index without hand-editing the database, which for an
evidence tool is a custody defect, not an inconvenience.

The requests are issued from several threads, which is the same shape as two
browser tabs, a second client, or one client retrying after a timeout. No UI is
involved: the invariant has to hold at the API boundary, because the worker,
scripts and TN's integrations all go through it.

`widen_race_window` makes the window deterministic. In the fixed code the
requests serialise and the window never opens; in the unfixed code all four
callers pass the check before any of them inserts. Without it this module would
be a coin flip on the unfixed code — and a test that only sometimes demonstrates
the bug proves nothing.

These tests fail on the unfixed code:
  * `test_concurrent_retry_admits_exactly_one_job` — 4 jobs, not 1.
  * `test_the_losing_caller_gets_409_not_500` — 500s, not 409s.
  * `test_database_rejects_a_second_active_job_written_by_hand` — no index.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from conftest import ADMIN_HEADERS, upload_ok

CONCURRENT_RETRIES = 4

# How long a caller waits at the barrier for its rivals. The unfixed code has
# four arrivals and releases immediately; the fixed code has one arrival and
# waits this out. Long enough to be reliable on a loaded machine, short enough
# that the fixed path is not slowed noticeably.
BARRIER_TIMEOUT_S = 1.5


def active_jobs(recording_id: str | None = None) -> list[str]:
    """Statuses of active jobs, straight from the database.

    Read through SQL rather than the API on purpose: the assertion is about what
    the *database* holds, and `/jobs` is itself application code under test.
    """
    from app import db

    sql = "SELECT id, status FROM jobs WHERE status IN ('queued', 'running')"
    params: dict = {}
    if recording_id is not None:
        sql += " AND recording_id = :recording_id"
        params["recording_id"] = recording_id
    with db.engine.connect() as conn:
        return [row[1] for row in conn.execute(text(sql), params).all()]


def all_jobs(recording_id: str) -> list[tuple[str, str, str | None]]:
    from app import db

    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, error FROM jobs WHERE recording_id = :recording_id"
                " ORDER BY created_at, id"
            ),
            {"recording_id": recording_id},
        ).all()
    return [(row[0], row[1], row[2]) for row in rows]


@pytest.fixture
def widen_race_window(monkeypatch):
    """Hold every caller of `retry` between "no job is active" and the insert.

    `audit.record` is called inside that window on both the old and the new
    code path, and it is a side effect rather than a decision, so delaying it
    does not change what the endpoint means — it only makes the interleaving
    that the defect depends on happen every run instead of one run in fifty.

    The barrier is best-effort: it releases as soon as all callers arrive, or
    after `BARRIER_TIMEOUT_S` if fewer do. That asymmetry is the point. The
    unfixed code has every caller in the window at once, so the barrier opens
    instantly and all four insert. The fixed code lets one caller through the
    insert and rejects the rest before they ever reach this hook, so the one
    caller that does arrive waits out the timeout and proceeds alone.
    """
    from app import audit as audit_module

    barrier = threading.Barrier(CONCURRENT_RETRIES, timeout=BARRIER_TIMEOUT_S)
    original_record = audit_module.record

    def record_with_barrier(*args, **kwargs):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            # Fewer than CONCURRENT_RETRIES callers reached the window: the
            # others were already refused, which is the fixed behaviour.
            pass
        return original_record(*args, **kwargs)

    monkeypatch.setattr(audit_module, "record", record_with_barrier)
    return barrier


@pytest.fixture
def clear_recording(client, witness_sample):
    """A fully processed exhibit with no active job and no reviewed segments.

    `process_inline` is on in tests, so a normal upload finishes before it
    returns — the starting state for a retry is "nothing running".
    """
    record = upload_ok(client, witness_sample)
    assert active_jobs(record["id"]) == [], "upload must not leave a job active"
    return record


def _retry_four(client, recording_id: str):
    """Issue `CONCURRENT_RETRIES` retries at once, recording server errors as 500.

    An exception escaping a request is a 500 to a real client, but the test
    transport raises it into the calling thread instead. Catching it here keeps
    the assertion message readable ("500 in the codes") rather than burying it
    under a traceback from a worker thread.
    """

    def retry(_index: int):
        try:
            return client.post(
                f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS
            )
        except Exception as exc:  # noqa: BLE001 - deliberately mapping to 500
            return _ServerError(exc)

    with ThreadPoolExecutor(max_workers=CONCURRENT_RETRIES) as pool:
        return list(pool.map(retry, range(CONCURRENT_RETRIES)))


class _ServerError:
    """Stand-in for a response when the request raised instead of returning."""

    status_code = 500
    text = "request raised"

    def __init__(self, exc: Exception):
        self.text = f"request raised: {type(exc).__name__}: {exc}"

    def json(self) -> dict:  # pragma: no cover - only reached on failure
        return {"detail": self.text}


@pytest.fixture
def queued_mode(set_env):
    """Turn off inline processing so a winning retry leaves its job `queued`.

    This is the deployment shape (worker process, `run_pending_jobs`), and it is
    the shape the invariant is about. With inline processing on, the winner runs
    the entire transcription+embedding pipeline *inside* the request, so by the
    time a slow straggler arrives the job is already `succeeded` and starting a
    second run is legitimate — the exhibit genuinely has no active job. That is
    correct behaviour, but it makes "how many callers got 200" a race with the
    pipeline rather than with the invariant, so the two concurrency tests pin the
    job in `queued` instead and assert on what the database holds.
    """
    set_env(PROCESS_INLINE="0")


def test_concurrent_retry_admits_exactly_one_job(
    client, clear_recording, widen_race_window, queued_mode
):
    recording_id = clear_recording["id"]
    before = len(all_jobs(recording_id))  # the finished upload job

    responses = _retry_four(client, recording_id)

    codes = sorted(response.status_code for response in responses)
    assert codes.count(200) == 1, f"expected exactly one winner, got {codes}"

    # The invariant: whatever the API returned, the database must not hold more
    # than one active job for this exhibit.
    statuses = active_jobs(recording_id)
    assert statuses == ["queued"], f"duplicate pipeline runs: {statuses}"

    # Exactly one job row was created by the burst — not four.
    assert len(all_jobs(recording_id)) == before + 1, all_jobs(recording_id)


def test_a_queued_job_can_be_completed_and_then_replaced(client, clear_recording, queued_mode):
    """The exhibit must stay usable: finish the queued run, then retry again."""
    from app.pipeline import jobs as job_pipeline

    recording_id = clear_recording["id"]
    accepted = client.post(
        f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS
    )
    assert accepted.status_code == 200, accepted.text
    assert active_jobs(recording_id) == ["queued"]

    # Second request while it is still queued: the documented 409.
    blocked = client.post(f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS)
    assert blocked.status_code == 409

    # The worker drains it, and then a retry is allowed again — the invariant
    # must not have hardened into "one job per exhibit, ever".
    queued_id = next(job_id for job_id, status, _ in all_jobs(recording_id) if status == "queued")
    job_pipeline.run_job(queued_id)
    assert active_jobs(recording_id) == []

    again = client.post(f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS)
    assert again.status_code == 200, again.text
    assert len(all_jobs(recording_id)) == 3  # upload + first retry + this one


def test_the_losing_caller_gets_409_not_500(
    client, clear_recording, widen_race_window, queued_mode
):
    """A loser must see what a late caller sees, not a server error."""
    recording_id = clear_recording["id"]

    responses = _retry_four(client, recording_id)

    codes = sorted(response.status_code for response in responses)
    assert 500 not in codes, f"concurrency leaked a server error: {codes}"

    losers = [r for r in responses if r.status_code == 409]
    assert len(losers) == CONCURRENT_RETRIES - 1, (
        f"expected {CONCURRENT_RETRIES - 1} conflicts, got {codes}"
    )
    for response in losers:
        assert response.json()["detail"] == (
            "A processing job is already queued or running."
        )


def test_a_sequential_retry_while_active_is_still_409(client, clear_recording):
    """Baseline the guard must not change: the documented single-caller case."""
    from app.db import session_scope
    from app.models import Job

    recording_id = clear_recording["id"]
    with session_scope() as session:
        session.add(Job(recording_id=recording_id, kind="process", status="running"))

    response = client.post(
        f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS
    )
    assert response.status_code == 409
    assert "queued or running" in response.json()["detail"]


def test_finished_jobs_still_accumulate(client, clear_recording):
    """The invariant is "one *active* job", not "one job" — history must grow."""
    recording_id = clear_recording["id"]
    before = len(all_jobs(recording_id))

    response = client.post(
        f"/api/recordings/{recording_id}/retry", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text

    after = all_jobs(recording_id)
    assert len(after) == before + 1, f"job history did not grow: {after}"
    assert after[-1][1] == "succeeded"


def test_database_rejects_a_second_active_job_written_by_hand(client, clear_recording):
    """The guarantee is in the schema, so it also covers code that bypasses the API."""
    from sqlalchemy.exc import IntegrityError

    from app import db
    from app.db import session_scope
    from app.models import Job

    recording_id = clear_recording["id"]
    with session_scope() as session:
        session.add(Job(recording_id=recording_id, kind="process", status="running"))

    with pytest.raises(IntegrityError), session_scope() as session:
        session.add(Job(recording_id=recording_id, kind="process", status="queued"))

    with db.engine.connect() as conn:
        index_names = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'")
            ).all()
        }
    assert db.ACTIVE_JOB_UNIQUE_INDEX in index_names
