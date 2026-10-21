"""Migration guard: the active-job index must be installable on a damaged database.

A customer's database created before this fix may already hold several active
jobs for one recording — that is the defect itself, and it is what made an
exhibit permanently un-reprocessable. `CREATE UNIQUE INDEX` fails on such data,
so the migration has to repair it first. These tests build that legacy shape by
hand and run the migration against it, twice, to prove it is idempotent.

The migration is exercised on a throwaway engine via monkeypatch rather than on
the shared test database, because the point is what it does to a *pre-existing*
schema and to pre-existing bad rows.

Two behaviours are guarded here that are easy to get wrong:

* Job rows are audit surface. The repair must mark duplicates `failed` and keep
  them, never delete them — a vanished job row is a custody gap, and it is
  precisely the record of the duplicate run.
* The index is *partial*. A plain unique index on `recording_id` would reject a
  second finished job and stop job history accumulating, so the predicate is
  asserted to exist rather than just the uniqueness.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app import db

LEGACY_SCHEMA = """
CREATE TABLE recordings (
    id VARCHAR(40) NOT NULL PRIMARY KEY,
    filename VARCHAR(512) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    original_path VARCHAR(1024) NOT NULL,
    working_path VARCHAR(1024),
    denoised_path VARCHAR(1024),
    sha256 VARCHAR(64) NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_s FLOAT,
    sample_rate INTEGER,
    channels INTEGER,
    status VARCHAR(24) NOT NULL,
    error TEXT,
    processing_meta JSON NOT NULL,
    uploaded_by VARCHAR(128) NOT NULL,
    recorded_at DATETIME,
    created_at DATETIME NOT NULL,
    case_id VARCHAR(40)
);
CREATE TABLE jobs (
    id VARCHAR(40) NOT NULL PRIMARY KEY,
    recording_id VARCHAR(40) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    progress FLOAT NOT NULL,
    error TEXT,
    detail JSON NOT NULL,
    attempts INTEGER NOT NULL,
    actor VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    FOREIGN KEY(recording_id) REFERENCES recordings (id) ON DELETE CASCADE
);
CREATE INDEX ix_jobs_recording_id ON jobs (recording_id);
CREATE INDEX ix_jobs_status ON jobs (status);
"""


def _legacy_engine(tmp_path: Path):
    """A database with the pre-fix schema and the defect's damaged rows.

    Reproduces the audited state exactly: one exhibit with four jobs created
    within 100 ms, one of which finished and three of which are stuck `running`.
    A second, healthy exhibit is included so the repair is proven to be
    per-recording rather than a global truncation.
    """
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")

    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))

        for recording_id in ("rec_stuck", "rec_healthy"):
            conn.execute(
                text(
                    "INSERT INTO recordings (id, filename, source_type, original_path,"
                    " sha256, size_bytes, status, processing_meta, uploaded_by, created_at)"
                    " VALUES (:id, 'tape.wav', 'cassette', '/media/tape.wav',"
                    " 'abc', 1024, 'processing', '{}', 'tech', '2026-10-20 22:49:00')"
                ),
                {"id": recording_id},
            )

        # The defect: four concurrent retries, one winner and three stranded.
        rows = [
            ("job_a5a0ecde", "rec_stuck", "succeeded", "completed",
             "2026-10-20 22:50:05.598815", "2026-10-20 22:50:05.597398"),
            ("job_2f27b0b5", "rec_stuck", "running", "transcribing",
             "2026-10-20 22:50:05.606768", "2026-10-20 22:50:05.606177"),
            ("job_485665b3", "rec_stuck", "running", "transcribing",
             "2026-10-20 22:50:05.666908", "2026-10-20 22:50:05.660983"),
            ("job_1096d935", "rec_stuck", "running", "transcribing",
             "2026-10-20 22:50:05.692853", "2026-10-20 22:50:05.691813"),
            # A healthy exhibit: one finished job, nothing active.
            ("job_healthy1", "rec_healthy", "succeeded", "completed",
             "2026-10-20 21:00:00.000000", "2026-10-20 20:59:59.000000"),
        ]
        for job_id, recording_id, status, stage, created_at, started_at in rows:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, recording_id, kind, status, stage, progress,"
                    " detail, attempts, actor, created_at, started_at)"
                    " VALUES (:id, :recording_id, 'process', :status, :stage, 0.45,"
                    " '{}', 1, 'tech', :created_at, :started_at)"
                ),
                {
                    "id": job_id,
                    "recording_id": recording_id,
                    "status": status,
                    "stage": stage,
                    "created_at": created_at,
                    "started_at": started_at,
                },
            )
    return engine


@pytest.fixture
def legacy_engine(tmp_path: Path, monkeypatch):
    engine = _legacy_engine(tmp_path)
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _jobs(engine, recording_id: str) -> list[tuple[str, str, str, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, stage, error FROM jobs WHERE recording_id = :recording_id"
                " ORDER BY created_at, id"
            ),
            {"recording_id": recording_id},
        ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _active(engine, recording_id: str) -> list[str]:
    with engine.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT id FROM jobs WHERE recording_id = :recording_id"
                    " AND status IN ('queued', 'running')"
                ),
                {"recording_id": recording_id},
            ).all()
        ]


def test_migration_repairs_duplicates_then_creates_the_index(legacy_engine):
    assert len(_active(legacy_engine, "rec_stuck")) == 3, "fixture precondition"

    db._ensure_active_job_uniqueness()

    # Exactly one active job survives, and it is the newest of the four.
    assert _active(legacy_engine, "rec_stuck") == ["job_1096d935"]

    # Nothing was deleted: all five rows are still there.
    stuck = _jobs(legacy_engine, "rec_stuck")
    assert len(stuck) == 4, f"job history was truncated: {stuck}"

    # The retired rows are marked failed, and say why in terms an auditor can read.
    retired = {job_id: (status, error) for job_id, status, _stage, error in stuck
               if job_id != "job_1096d935"}
    for job_id in ("job_2f27b0b5", "job_485665b3"):
        status, error = retired[job_id]
        assert status == "failed"
        assert error is not None and "Superseded" in error
        assert "3 concurrently started jobs" in error

    # The winning job and the earlier *finished* job are untouched.
    by_id = {job_id: (status, stage, error) for job_id, status, stage, error in stuck}
    assert by_id["job_1096d935"][0] == "running"
    assert by_id["job_1096d935"][2] is None
    assert by_id["job_a5a0ecde"][0] == "succeeded"

    # The healthy exhibit was not touched at all.
    assert _jobs(legacy_engine, "rec_healthy") == [
        ("job_healthy1", "succeeded", "completed", None)
    ]

    # The invariant is now enforced by the database itself.
    with pytest.raises(IntegrityError), legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, recording_id, kind, status, stage, progress,"
                " detail, attempts, actor, created_at) VALUES ('job_clash', 'rec_stuck',"
                " 'process', 'queued', 'queued', 0.0, '{}', 0, 'tech',"
                " '2026-10-20 23:00:00')"
            )
        )


def test_index_is_partial_so_finished_jobs_still_accumulate(legacy_engine):
    """A plain unique index on recording_id would break job history — assert the WHERE."""
    db._ensure_active_job_uniqueness()

    with legacy_engine.begin() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
            {"name": db.ACTIVE_JOB_UNIQUE_INDEX},
        ).scalar_one()
    assert "WHERE" in ddl.upper(), ddl
    assert "queued" in ddl and "running" in ddl, ddl

    # Two *finished* jobs for the same exhibit are still allowed, and so is a
    # third once the active one finishes.
    with legacy_engine.begin() as conn:
        for job_id in ("job_done2", "job_done3"):
            conn.execute(
                text(
                    "INSERT INTO jobs (id, recording_id, kind, status, stage, progress,"
                    " detail, attempts, actor, created_at) VALUES (:id, 'rec_stuck',"
                    " 'process', 'failed', 'failed', 1.0, '{}', 1, 'tech',"
                    " '2026-10-21 00:00:00')"
                ),
                {"id": job_id},
            )
    assert len(_jobs(legacy_engine, "rec_stuck")) == 6


def test_migration_is_idempotent(legacy_engine):
    db._ensure_active_job_uniqueness()
    first = _jobs(legacy_engine, "rec_stuck")

    # Second and third run: no further retirement, no error, no schema change.
    db._ensure_active_job_uniqueness()
    db._ensure_active_job_uniqueness()

    assert _jobs(legacy_engine, "rec_stuck") == first
    assert _active(legacy_engine, "rec_stuck") == ["job_1096d935"]


def test_migration_is_a_noop_once_the_index_exists(legacy_engine):
    db._ensure_active_job_uniqueness()
    with legacy_engine.connect() as conn:
        assert db._active_job_index_exists(conn) is True

    # Re-introducing damage must not be "repaired" again: the index is what
    # protects the invariant now, and it cannot be bypassed.
    with legacy_engine.begin() as conn:
        conn.execute(text("DROP INDEX " + db.ACTIVE_JOB_UNIQUE_INDEX))
    with legacy_engine.connect() as conn:
        assert db._active_job_index_exists(conn) is False

    db._ensure_active_job_uniqueness()
    with legacy_engine.connect() as conn:
        assert db._active_job_index_exists(conn) is True
    assert _active(legacy_engine, "rec_stuck") == ["job_1096d935"], "no double-retirement"


def test_migration_on_a_clean_database_writes_nothing(legacy_engine):
    """A healthy customer database must not be modified by an upgrade."""
    with legacy_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE recording_id = 'rec_stuck'"))
        conn.execute(
            text(
                "INSERT INTO jobs (id, recording_id, kind, status, stage, progress,"
                " detail, attempts, actor, created_at) VALUES ('job_only', 'rec_stuck',"
                " 'process', 'succeeded', 'completed', 1.0, '{}', 1, 'tech',"
                " '2026-10-20 22:50:05')"
            )
        )
    before = _jobs(legacy_engine, "rec_stuck")

    db._ensure_active_job_uniqueness()

    assert _jobs(legacy_engine, "rec_stuck") == before
    with legacy_engine.connect() as conn:
        assert db._active_job_index_exists(conn) is True


def test_fresh_database_has_the_index_from_the_table_definition():
    """`create_all` must produce the index, not rely on the migration."""
    with db.engine.connect() as conn:
        names = {index["name"] for index in __import__("sqlalchemy").inspect(conn).get_indexes("jobs")}
    assert db.ACTIVE_JOB_UNIQUE_INDEX in names


def test_live_database_rejects_a_duplicate_active_job(client, witness_sample):
    """End-to-end: the live schema refuses a hand-written duplicate."""
    from conftest import upload_ok

    recording = upload_ok(client, witness_sample)

    insert = text(
        "INSERT INTO jobs (id, recording_id, kind, status, stage, progress,"
        " detail, attempts, actor, created_at) VALUES (:id, :recording_id,"
        " 'process', :status, 'queued', 0.0, '{}', 0, 'hand',"
        " '2026-01-01 00:00:00')"
    )
    params = {"recording_id": recording["id"]}

    # First active job is accepted: uploading finished one run, and history is
    # allowed to hold more than one row.
    with db.engine.begin() as conn:
        conn.execute(insert, {**params, "id": "job_hand_1", "status": "queued"})

    # The second active job for the same exhibit is refused by the schema.
    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(insert, {**params, "id": "job_hand_2", "status": "running"})
