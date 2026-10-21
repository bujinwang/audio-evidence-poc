"""Migration guard: the unique index must be installable on a damaged database.

A customer's database created before this fix may already contain a segment
whose revision chain holds the same version number several times — that is the
defect itself. `CREATE UNIQUE INDEX` fails on such data, so the migration has to
repair the chain first. These tests build that exact legacy shape by hand and
run the migration against it, twice, to prove it is idempotent.

The migration is exercised on a throwaway engine via monkeypatch rather than on
the shared test database, because the point is what it does to a *pre-existing*
schema that `create_all` cannot have produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import db

LEGACY_SCHEMA = """
CREATE TABLE segments (
    id VARCHAR(40) NOT NULL PRIMARY KEY,
    transcript_id VARCHAR(40) NOT NULL,
    recording_id VARCHAR(40) NOT NULL,
    idx INTEGER NOT NULL,
    start_s FLOAT NOT NULL,
    end_s FLOAT NOT NULL,
    text TEXT NOT NULL,
    ai_text TEXT NOT NULL,
    speaker VARCHAR(64),
    words JSON,
    review_status VARCHAR(24) NOT NULL,
    version INTEGER NOT NULL,
    updated_at DATETIME NOT NULL,
    verified_by VARCHAR(128),
    verified_at DATETIME
);
CREATE TABLE revisions (
    id VARCHAR(40) NOT NULL PRIMARY KEY,
    segment_id VARCHAR(40) NOT NULL,
    version INTEGER NOT NULL,
    prev_text TEXT NOT NULL,
    new_text TEXT NOT NULL,
    actor VARCHAR(128) NOT NULL,
    action VARCHAR(24) NOT NULL,
    note TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(segment_id) REFERENCES segments (id) ON DELETE CASCADE
);
CREATE INDEX ix_revisions_segment_id ON revisions (segment_id);
"""


def _legacy_engine(tmp_path: Path):
    """A database with the pre-fix schema and a duplicated revision chain."""
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")

    with engine.begin() as conn:
        for statement in LEGACY_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))

        # Two segments so the renumbering is proven to be per segment, not global.
        # `seg_dup` kept version 2 even though its chain holds five rows (that is
        # the defect). `seg_clean` is a healthy segment: chain 2, 3, 4 and a row
        # version that already matches its chain head.
        for segment_id, version in (("seg_dup", 2), ("seg_clean", 4)):
            conn.execute(
                text(
                    "INSERT INTO segments (id, transcript_id, recording_id, idx, start_s, end_s,"
                    " text, ai_text, review_status, version, updated_at)"
                    " VALUES (:id, 'tr_1', 'rec_1', 0, 0.0, 1.0, 'text', 'ai text',"
                    " 'corrected', :version, '2026-01-01 00:00:00')"
                ),
                {"id": segment_id, "version": version},
            )

    with engine.begin() as conn:
        # seg_dup: five concurrent edits each claimed version 2 (the defect),
        # distinguished only by created_at. The segment row kept version 2 too,
        # so it does not even match its own chain.
        for index in range(5):
            conn.execute(
                text(
                    "INSERT INTO revisions (id, segment_id, version, prev_text, new_text, actor,"
                    " action, note, created_at) VALUES (:id, 'seg_dup', 2, :prev, :new, :actor,"
                    " 'edit', :note, :created_at)"
                ),
                {
                    "id": f"rev_dup_{index}",
                    "prev": f"pass {index}",
                    "new": f"pass {index + 1}",
                    "actor": f"reviewer{index}",
                    "note": f"tab {index}",
                    "created_at": f"2026-01-01 00:00:0{index}",
                },
            )
        # seg_clean: an already-correct chain that the migration must not touch.
        for index, version in enumerate((2, 3, 4)):
            conn.execute(
                text(
                    "INSERT INTO revisions (id, segment_id, version, prev_text, new_text, actor,"
                    " action, note, created_at) VALUES (:id, 'seg_clean', :version, 'a', 'b',"
                    " 'reviewer', 'edit', NULL, :created_at)"
                ),
                {
                    "id": f"rev_clean_{index}",
                    "version": version,
                    "created_at": f"2026-01-01 00:00:0{index}",
                },
            )
    return engine


@pytest.fixture
def legacy_engine(tmp_path: Path, monkeypatch):
    engine = _legacy_engine(tmp_path)
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _versions(engine, segment_id: str) -> list[int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT version FROM revisions WHERE segment_id = :id"
                " ORDER BY created_at, id"
            ),
            {"id": segment_id},
        ).all()
    return [row[0] for row in rows]


def _segment_version(engine, segment_id: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT version FROM segments WHERE id = :id"), {"id": segment_id}
        ).scalar_one()


def test_migration_repairs_duplicates_then_creates_the_unique_index(legacy_engine):
    assert _versions(legacy_engine, "seg_dup") == [2, 2, 2, 2, 2], "fixture precondition"
    assert _segment_version(legacy_engine, "seg_dup") == 2

    db._ensure_revision_version_uniqueness()

    # Contiguous, in the order the corrections were actually made.
    assert _versions(legacy_engine, "seg_dup") == [2, 3, 4, 5, 6]
    # The segment now agrees with the head of its own chain.
    assert _segment_version(legacy_engine, "seg_dup") == 6

    # A healthy chain is left exactly as it was.
    assert _versions(legacy_engine, "seg_clean") == [2, 3, 4]
    assert _segment_version(legacy_engine, "seg_clean") == 4

    # No correction was lost and none of its content was rewritten.
    with legacy_engine.connect() as conn:
        actors = [
            row[0]
            for row in conn.execute(
                text("SELECT actor FROM revisions WHERE segment_id='seg_dup' ORDER BY version")
            ).all()
        ]
    assert actors == [f"reviewer{i}" for i in range(5)]

    # The invariant is now enforced by the database itself.
    with pytest.raises(IntegrityError), legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO revisions (id, segment_id, version, prev_text, new_text, actor,"
                " action, note, created_at) VALUES ('rev_clash', 'seg_dup', 2, 'a', 'b',"
                " 'reviewer', 'edit', NULL, '2026-01-02 00:00:00')"
            )
        )


def test_migration_is_idempotent(legacy_engine):
    db._ensure_revision_version_uniqueness()
    first = _versions(legacy_engine, "seg_dup")

    # Second and third run: no renumbering, no error, no schema change.
    db._ensure_revision_version_uniqueness()
    db._ensure_revision_version_uniqueness()

    assert _versions(legacy_engine, "seg_dup") == first
    assert _versions(legacy_engine, "seg_dup") == [2, 3, 4, 5, 6]


def test_migration_is_a_noop_once_the_constraint_exists(legacy_engine):
    db._ensure_revision_version_uniqueness()
    with legacy_engine.connect() as conn:
        assert db._revision_unique_index_exists(conn) is True

    # Simulate a database built fresh by `create_all`, where the constraint
    # arrives with the table definition rather than from the migration.
    with legacy_engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_revision_segment_version"))
    with pytest.raises(AssertionError), legacy_engine.connect() as conn:
        assert db._revision_unique_index_exists(conn) is True

    db._ensure_revision_version_uniqueness()
    with legacy_engine.connect() as conn:
        assert db._revision_unique_index_exists(conn) is True
    assert _versions(legacy_engine, "seg_dup") == [2, 3, 4, 5, 6], "no re-renumbering"


def test_fresh_database_reports_the_constraint_from_the_table_definition():
    """`create_all` must produce the unique constraint, not need the migration."""
    with db.engine.connect() as conn:
        uniques = inspect(conn).get_unique_constraints("revisions")
    names = {constraint["name"] for constraint in uniques}
    assert db.REVISION_UNIQUE_INDEX in names


def test_live_database_rejects_a_duplicate_revision(client, witness_sample):
    """End-to-end: the live schema refuses a hand-written duplicate."""
    from conftest import REVIEWER_HEADERS, upload_ok

    recording = upload_ok(client, witness_sample)
    segment = client.get(f"/api/recordings/{recording['id']}/transcript").json()["segments"][0]
    client.patch(
        f"/api/segments/{segment['id']}",
        json={"text": "One correction."},
        headers=REVIEWER_HEADERS,
    )

    with pytest.raises(IntegrityError), db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO revisions (id, segment_id, version, prev_text, new_text, actor,"
                " action, note, created_at) VALUES ('rev_forced_dup', :segment_id, 2, 'a',"
                " 'b', 'hand', 'edit', NULL, '2026-01-01 00:00:00')"
            ),
            {"segment_id": segment["id"]},
        )
