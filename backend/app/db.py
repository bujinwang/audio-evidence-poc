"""Database engine, session factory, and schema bootstrap.

PostgreSQL is the target (docker-compose). SQLite is supported so the demo and
the test suite can run with zero infrastructure — every query in the app is
dialect-aware, and the search layer picks the matching full-text strategy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
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
    _ensure_search_indexes()


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


def healthcheck() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover - defensive
        logger.exception("database healthcheck failed")
        return False
