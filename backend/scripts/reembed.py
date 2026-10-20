#!/usr/bin/env python3
"""Rebuild the semantic index for the whole corpus with the current embedder.

Why this exists: the semantic store persists one vector per segment in
``segment_embeddings`` (model / dim / vector JSON). The embedder is chosen at
*transcription* time via ``EMBEDDER``/``EMBEDDING_MODEL``. If those change — for
example installing ``sentence-transformers`` so the app stops using the
``hashing-lexical-v2`` fallback — every stored vector is suddenly in the wrong
space: a 384-dim query compared against a 512-dim corpus returns nothing, and
segments embedded under the old model are simply missed.

This script re-embeds **every** segment of **every** transcript with whatever
embedder is configured now, overwriting row-by-row (``segment_id`` is the
primary key) so there is no mixing of dimensions. It is idempotent: run it
again and you get the same result. It also pushes into the active vector store
(local DB-backed, or Qdrant if configured).

Usage:
    .venv/bin/python scripts/reembed.py                 # auto embedder
    .venv/bin/python scripts/reembed.py --embedder auto
    .venv/bin/python scripts/reembed.py --model sentence-transformers/all-MiniLM-L6-v2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# scripts/ -> backend/ ; make the `app` package importable without PYTHONPATH.
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app import audit  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, SessionLocal  # noqa: E402
from app.models import Recording, Segment, SegmentEmbedding, Transcript  # noqa: E402
from app.pipeline.embed import get_embedder  # noqa: E402
from app.pipeline.vectorstore import VectorItem, get_vector_store  # noqa: E402

EMBED_BATCH = 32


def reembed(embedder, vector_store, session) -> dict:
    """Re-embed every segment in the database. Returns a summary dict."""
    transcripts = list(session.execute(select(Transcript)).scalars())
    written = 0
    skipped_empty = 0
    recordings_touched: set[str] = set()

    for transcript in transcripts:
        recording = session.get(Recording, transcript.recording_id)
        if recording is None:
            continue
        segments = list(
            session.execute(
                select(Segment)
                .where(Segment.transcript_id == transcript.id)
                .order_by(Segment.idx)
            ).scalars()
        )
        items: list[VectorItem] = []
        for offset in range(0, len(segments), EMBED_BATCH):
            chunk = segments[offset : offset + EMBED_BATCH]
            texts = [(s.text or "").strip() for s in chunk]
            vectors = embedder.embed_documents(texts)
            for segment, text, vector in zip(chunk, texts, vectors):
                if not text:
                    # An empty segment can never match a semantic query; storing a
                    # zero/NaN vector only pollutes the index. Skip it.
                    skipped_empty += 1
                    continue
                session.merge(
                    SegmentEmbedding(
                        segment_id=segment.id,
                        recording_id=recording.id,
                        model=embedder.name,
                        dim=len(vector),
                        vector=json.dumps(vector),
                    )
                )
                recordings_touched.add(recording.id)
                items.append(
                    VectorItem(
                        segment_id=segment.id,
                        recording_id=recording.id,
                        vector=vector,
                        payload={
                            "recording_id": recording.id,
                            "variant": transcript.variant,
                            "source_type": recording.source_type,
                            "speaker": segment.speaker or "",
                            "review_status": segment.review_status,
                            "recorded_at": int(recording.recorded_at.timestamp())
                            if recording.recorded_at
                            else 0,
                        },
                    )
                )
                written += 1
            session.commit()

        if items:
            try:
                vector_store.upsert(items)
            except Exception as exc:  # pragma: no cover - remote store
                print(f"  ! vector-store upsert failed for {recording.id}: {exc}", file=sys.stderr)

    # One audit entry for the whole operation: the semantic index was rebuilt.
    audit.record(
        session,
        "system.reembed",
        "semantic.reindex",
        entity_type="corpus",
        entity_id="semantic_index",
        detail={
            "embedder": embedder.name,
            "is_semantic": embedder.is_semantic,
            "dim": embedder.dim,
            "vectors_written": written,
            "skipped_empty": skipped_empty,
            "recordings": len(recordings_touched),
        },
    )
    session.commit()

    return {
        "embedder": embedder.name,
        "is_semantic": embedder.is_semantic,
        "dim": embedder.dim,
        "vectors_written": written,
        "skipped_empty": skipped_empty,
        "recordings": len(recordings_touched),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--embedder",
        default=None,
        help="Embedder preference (default: EMBEDDER env, else 'auto').",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Embedding model name (default: EMBEDDING_MODEL env).",
    )
    args = parser.parse_args()

    settings = get_settings()
    embedder = get_embedder(args.embedder or settings.embedder, args.model or settings.embedding_model)
    ok, reason = embedder.available()
    print(f"Embedder: {embedder.name} (is_semantic={embedder.is_semantic}, dim={embedder.dim})")
    if not ok:
        print(f"ERROR: embedder unavailable: {reason}", file=sys.stderr)
        return 2

    vector_store = get_vector_store(
        SessionLocal(),
        preference=settings.vector_store,
        url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        dim=embedder.dim,
    )
    print(f"Vector store: {vector_store.name}")

    with SessionLocal() as session:
        summary = reembed(embedder, vector_store, session)

    print("Re-embedding complete:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    if not summary["is_semantic"]:
        print(
            "WARNING: active embedder is the lexical fallback (not a semantic model). "
            "Install sentence-transformers for true AI semantic search.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
