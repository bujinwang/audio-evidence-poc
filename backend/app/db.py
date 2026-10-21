"""Database engine, session factory, and schema bootstrap.

PostgreSQL is the target (docker-compose). SQLite is supported so the demo and
the test suite can run with zero infrastructure — every query in the app is
dialect-aware, and the search layer picks the matching full-text strategy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import bindparam, create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_settings = get_settings()

_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if _settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    _engine_kwargs.update({"pool_size": 5, "max_overflow": 10})

engine = create_engine(_settings.database_url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


if _settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers and scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers before create_all)

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_search_indexes()
    _ensure_revision_version_uniqueness()
    _ensure_active_job_uniqueness()


def _ensure_columns() -> None:
    """Additive column migration.

    `create_all` creates missing tables but never alters existing ones, and this
    project deliberately carries no Alembic. New nullable columns are added here,
    idempotently, so an existing demo database keeps working.
    """
    wanted = [("recordings", "case_id", "VARCHAR(40)")]
    with engine.begin() as conn:
        for table, column, ddl in wanted:
            if _settings.is_sqlite:
                existing = {
                    row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
                }
                if column in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            else:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
                )
            logger.info("added column %s.%s", table, column)


def _ensure_search_indexes() -> None:
    """Create the keyword-search structures for the active dialect."""
    with engine.begin() as conn:
        if _settings.is_sqlite:
            # Contentless FTS5 index maintained by the application layer so that
            # edits to a segment's text are reflected immediately.
            #
            # `porter` stemming matters for correctness, not just recall. With
            # the plain `unicode61` tokenizer every word form is its own token,
            # so `truck` could not find "trucks", `walk` could not find
            # "walking", and `small` could not find "smaller" — a searcher had
            # to guess the exact word form in the transcript. PostgreSQL's
            # `english` dictionary already conflates these, so plain tokenizing
            # also made SQLite (the demo path) behave differently from the
            # deployment path. `porter` aligns the two.
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5("
                    "segment_id UNINDEXED, text, "
                    "tokenize='porter unicode61 remove_diacritics 2')"
                )
            )
            _migrate_fts_tokenizer(conn)
        else:
            # Expression index: stays correct on edit, no trigger bookkeeping.
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_segments_tsv "
                    "ON segments USING GIN (to_tsvector('english', text))"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_segments_recording "
                    "ON segments (transcript_id)"
                )
            )
    logger.debug("search indexes ensured (%s)", "sqlite" if _settings.is_sqlite else "postgres")


def _migrate_fts_tokenizer(conn) -> None:
    """Rebuild the FTS table if it was created with an older tokenizer.

    An existing database keeps whatever tokenizer it was created with —
    `CREATE VIRTUAL TABLE IF NOT EXISTS` is a no-op once the table exists. The
    index is derived data (rebuilt from `segments`), so dropping and recreating
    it is safe and needs no schema version bookkeeping.
    """
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='segments_fts'")
    ).first()
    if row is None or "porter" in (row[0] or "").lower():
        return

    logger.info("rebuilding FTS index with porter stemming (was unstemmed)")
    conn.execute(text("DROP TABLE segments_fts"))
    conn.execute(
        text(
            "CREATE VIRTUAL TABLE segments_fts USING fts5("
            "segment_id UNINDEXED, text, "
            "tokenize='porter unicode61 remove_diacritics 2')"
        )
    )
    rows = conn.execute(text("SELECT id, text FROM segments")).all()
    if rows:
        conn.execute(
            text("INSERT INTO segments_fts (segment_id, text) VALUES (:segment_id, :text)"),
            [{"segment_id": segment_id, "text": segment_text or ""} for segment_id, segment_text in rows],
        )


REVISION_UNIQUE_INDEX = "uq_revision_segment_version"


def _ensure_revision_version_uniqueness() -> None:
    """Enforce one revision per `(segment_id, version)` on an existing database.

    `Revision.__table_args__` declares the unique constraint, so a database
    created by `create_all` already has it. A database created by an earlier
    version of this app does not — and a database that ran the buggy
    read-modify-write allocation may already contain segments whose revision
    chain holds the same version number more than once. Creating the index on
    such a database would fail, so the duplicates are repaired first.

    Repair means: each segment's revision chain is numbered contiguously from 2
    in `created_at` order (version 1 is the machine original and has no revision
    row of its own), and `segments.version` is set to the newest number in that
    chain so the segment and its history agree again. Nothing is deleted — the
    corrections, their authors, notes and timestamps are all preserved; only the
    ambiguous numbers are reissued in the order the corrections were made.

    The whole thing is idempotent: it returns immediately once the constraint
    is in place, so a second startup does no work.
    """
    with engine.begin() as conn:
        if _revision_unique_index_exists(conn):
            return
        renumbered = _renumber_revision_chains(conn)
        _resync_segment_versions(conn)
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {REVISION_UNIQUE_INDEX} "
                "ON revisions (segment_id, version)"
            )
        )
        logger.info(
            "created unique index %s on revisions (segment_id, version)%s",
            REVISION_UNIQUE_INDEX,
            f", renumbered {renumbered} revision(s)" if renumbered else "",
        )


def _revision_unique_index_exists(conn) -> bool:
    """Is `(segment_id, version)` already unique, under any name?

    The name is not reliable: SQLite names the index from a table-level
    `UNIQUE` clause `sqlite_autoindex_revisions_N`, so the columns are what get
    checked. Both a unique constraint and a unique index count.
    """
    target = ["segment_id", "version"]
    inspector = inspect(conn)

    def columns(entry: dict) -> list[str]:
        return [str(name).lower() for name in entry.get("column_names") or []]

    for constraint in inspector.get_unique_constraints("revisions"):
        if columns(constraint) == target:
            return True
    for index in inspector.get_indexes("revisions"):
        if index.get("unique") and columns(index) == target:
            return True
    return False


def _renumber_revision_chains(conn) -> int:
    """Renumber every revision chain 2, 3, 4 … in creation order.

    Returns the number of rows whose version actually changed. A healthy
    database produces 0 and is not written to.

    Chains are processed one segment at a time so peak memory follows the
    largest chain rather than the whole `revisions` table — this runs against a
    customer database, not just the demo fixture.
    """
    segment_ids = [
        row[0]
        for row in conn.execute(
            text("SELECT DISTINCT segment_id FROM revisions ORDER BY segment_id")
        ).all()
    ]

    total = 0
    for segment_id in segment_ids:
        rows = conn.execute(
            text(
                "SELECT id, version FROM revisions WHERE segment_id = :segment_id "
                "ORDER BY created_at, id"
            ),
            {"segment_id": segment_id},
        ).all()

        updates: list[dict] = []
        expected = 2
        for revision_id, version in rows:
            if version != expected:
                updates.append({"revision_id": revision_id, "version": expected})
            expected += 1

        if updates:
            conn.execute(
                text("UPDATE revisions SET version = :version WHERE id = :revision_id"),
                updates,
            )
            total += len(updates)
    return total


def _resync_segment_versions(conn) -> None:
    """Raise `segments.version` to the head of its revision chain where needed.

    A segment that lost the allocation race kept whatever number the losing
    request computed, so it can sit below the chain it owns. The chain is the
    authority for the audit trail, so the segment is re-pointed at its head.
    Only ever raised, never lowered: a number that is already consistent stays
    untouched, so re-running this cannot rewrite a healthy database.
    """
    conn.execute(text("UPDATE segments SET version = 1 WHERE version IS NULL"))
    conn.execute(
        text(
            "UPDATE segments SET version = ("
            "  SELECT MAX(revisions.version) FROM revisions"
            "  WHERE revisions.segment_id = segments.id"
            ") WHERE version < ("
            "  SELECT MAX(revisions.version) FROM revisions"
            "  WHERE revisions.segment_id = segments.id"
            ")"
        )
    )


ACTIVE_JOB_UNIQUE_INDEX = "uq_job_active_recording"

# The condition that defines an "active" job. Shared by the declarative index in
# `models.Job` and the migration below, so both paths install the same rule.
# Written as raw SQL because a partial index predicate is dialect DDL, not an
# ORM expression, on both SQLite and PostgreSQL.
ACTIVE_JOB_PREDICATE = "status IN ('queued', 'running')"

# The same set as a Python tuple, for the ORM-side query. Kept adjacent to the
# predicate above so the two cannot drift into disagreeing about what "active"
# means.
ACTIVE_JOB_STATUSES = ("queued", "running")

# Why a stranded job was retired, kept verbatim in `jobs.error` so the audit
# trail explains itself instead of showing an unexplained `failed` row.
#
# "Superseded" here means the ONE-TIME migration below collapsed a pre-existing
# pile of duplicates so the index could be created. It is not a runtime policy:
# nothing supersedes an active job at request time (see `jobs.enqueue()`), and a
# job that a running process abandoned is retired only by an explicit operator
# call to `POST /api/jobs/{id}/reset`. A maintainer who reads this string as
# "the app takes over abandoned jobs" will not write the `/reset` call and will
# conclude the exhibit is permanently stuck.
DEDUPE_ERROR = (
    "Superseded: this recording had {count} concurrently started jobs for one "
    "exhibit. Kept the most recent and retired the rest so an active-job "
    "invariant could be enforced. No work was lost from the transcript; this "
    "row is retained as job history."
)

STALE_ERROR = (
    "Marked stale by an operator: the process running this job stopped before it "
    "reported a result (last stage '{stage}', started {started_at}). The row is "
    "retained as job history; re-submit this exhibit to process it again."
)


def _ensure_active_job_uniqueness() -> None:
    """Enforce at most one active (queued or running) job per recording.

    `Job.__table_args__` declares the partial unique index, so a database created
    by `create_all` already has it. A database created by an earlier version of
    this app does not — and one that served concurrent `POST /retry` requests
    may already hold several active jobs for a single recording, which is the
    defect that made an exhibit permanently un-reprocessable. Creating the index
    on such a database would fail, so the duplicates are repaired first.

    Repair means: the most recent active job per recording is kept (it is the
    one a caller would most plausibly consider "the" run, and it is the one that
    still matches the newest state of the recording); every older active job for
    that recording is marked `failed` with an explanatory `error` and a
    `finished_at`. Nothing is deleted — job rows are audit surface, and a
    vanished job row would be a custody gap, not a cleanup: it is exactly the
    record of the duplicate run this defect caused.

    Only rows that would collide are touched, so a healthy database produces 0
    writes. Idempotent: it returns immediately once the index is in place, so a
    second startup does no work.
    """
    with engine.begin() as conn:
        if _active_job_index_exists(conn):
            return
        retired = _retire_duplicate_active_jobs(conn)
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_JOB_UNIQUE_INDEX} "
                f"ON jobs (recording_id) WHERE {ACTIVE_JOB_PREDICATE}"
            )
        )
        logger.info(
            "created partial unique index %s on jobs (recording_id) where %s%s",
            ACTIVE_JOB_UNIQUE_INDEX,
            ACTIVE_JOB_PREDICATE,
            f", retired {retired} duplicate active job(s)" if retired else "",
        )


def _active_job_index_exists(conn) -> bool:
    """Is "at most one active job per recording" already enforced, under any name?

    Mirrors `_revision_unique_index_exists`: the columns plus the uniqueness are
    what get checked, because SQLAlchemy names an index declared in
    `__table_args__` after the table and columns while a hand-created index has
    whatever name it was given.

    A predicate must be present. An unconditional unique index on
    `recording_id` would reject a second *finished* job, which is a different
    (and wrong) invariant — accumulating job history is the point. The declared
    name is also accepted directly, because some dialects do not reflect an
    index predicate back through `get_indexes`, and re-running `CREATE UNIQUE
    INDEX IF NOT EXISTS` on every startup would be harmless but noisy.
    """
    inspector = inspect(conn)
    for index in inspector.get_indexes("jobs"):
        if index.get("name") == ACTIVE_JOB_UNIQUE_INDEX:
            return True
        columns = [str(name).lower() for name in index.get("column_names") or []]
        if (
            index.get("unique")
            and columns == ["recording_id"]
            and _index_predicate(index) is not None
        ):
            return True
    return False


def _index_predicate(index: dict) -> str | None:
    """Extract the WHERE clause SQLAlchemy reports for a partial index.

    The values used to be combined with `or`, which raises
    `TypeError: Boolean value of this clause is not defined` because
    `sqlite_where` / `postgresql_where` arrive as a SQLAlchemy `TextClause`
    and a `TextClause` deliberately refuses to be coerced to `bool`. Each
    candidate is therefore tested with an explicit `is None` instead.
    """
    for key in ("dialect_options", "postgresql_where", "sqlite_where"):
        value = index.get(key)
        if isinstance(value, dict):
            predicate = value.get("sqlite_where")
            if predicate is None:
                predicate = value.get("postgresql_where")
            value = predicate
        if value is not None:
            return str(value)
    return None


def _retire_duplicate_active_jobs(conn) -> int:
    """Mark all but the newest active job per recording `failed`. Returns the count.

    Written through the Core table object rather than raw SQL so the
    `finished_at` timestamp is bound as a `DateTime` with the dialect's own
    format — the alternative (formatting a string per dialect) is exactly the
    kind of thing that works on the demo path and breaks on PostgreSQL.

    Ordered by `created_at` then `id`, so the tie-break is total: the four rows
    this defect produced land within 100 ms of one another, and a
    non-deterministic winner would make the migration unreproducible.
    """
    from .models import Job, utcnow  # local: avoid a circular import at module load

    table = Job.__table__
    rows = conn.execute(
        select(table.c.recording_id, table.c.id, table.c.status, table.c.stage)
        .where(table.c.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(table.c.recording_id, table.c.created_at, table.c.id)
    ).all()

    by_recording: dict[str, list] = {}
    for row in rows:
        by_recording.setdefault(row.recording_id, []).append(row)

    finished_at = utcnow()
    updates: list[dict] = []
    for group in by_recording.values():
        if len(group) < 2:
            continue
        # Keep the newest; retire the rest. `group` is already in created_at
        # order, so the oldest duplicates are the ones that get retired.
        for row in group[:-1]:
            updates.append(
                {
                    "job_id": row.id,
                    "error": DEDUPE_ERROR.format(count=len(group))
                    + f" Retired while '{row.status}' at stage '{row.stage or 'unknown'}'.",
                    "finished_at": finished_at,
                }
            )

    if not updates:
        return 0

    conn.execute(
        table.update()
        .where(table.c.id == bindparam("job_id"))
        .values(
            status="failed",
            stage="superseded",
            error=bindparam("error"),
            finished_at=bindparam("finished_at", type_=table.c.finished_at.type),
        ),
        updates,
    )
    return len(updates)


def healthcheck() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover - defensive
        logger.exception("database healthcheck failed")
        return False
