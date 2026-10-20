"""Vector store abstraction: Qdrant in a full deployment, local cosine otherwise.

Both implementations take/return the same `(segment_id, score)` pairs, so the
search layer does not care which one is active. Vectors are always persisted in
`segment_embeddings` first, which means the index can be rebuilt (or migrated to
Qdrant) without re-running transcription.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..filters import SearchFilters
from ..models import Recording, Segment, SegmentEmbedding, Transcript
from ..query import apply_segment_filters

logger = logging.getLogger(__name__)


@dataclass
class VectorItem:
    segment_id: str
    recording_id: str
    vector: list[float]
    payload: dict


class VectorStore:
    name = "none"
    is_remote = False

    def upsert(self, items: list[VectorItem]) -> None:
        raise NotImplementedError

    def search(
        self, vector: list[float], limit: int, filters: SearchFilters
    ) -> list[tuple[str, float]]:
        raise NotImplementedError

    def delete_recording(self, recording_id: str) -> None:
        raise NotImplementedError

    def count(self) -> int:
        return 0

    def health(self) -> tuple[bool, str]:
        return False, "not configured"


class LocalVectorStore(VectorStore):
    """Exact cosine search in-process over vectors stored in the database.

    Correct and dependency-free; linear in corpus size, which is fine for a
    demo corpus of thousands of segments.
    """

    name = "local-numpy"

    def __init__(self, session: Session):
        self.session = session

    def upsert(self, items: list[VectorItem]) -> None:
        # Persistence is handled by the caller; nothing to do for the local index.
        return None

    def search(
        self, vector: list[float], limit: int, filters: SearchFilters
    ) -> list[tuple[str, float]]:
        import numpy as np

        stmt = (
            select(SegmentEmbedding.segment_id, SegmentEmbedding.vector)
            .join(Segment, Segment.id == SegmentEmbedding.segment_id)
            .join(Transcript, Transcript.id == Segment.transcript_id)
            .join(Recording, Recording.id == Segment.recording_id)
        )
        stmt = apply_segment_filters(stmt, filters)
        rows = self.session.execute(stmt).all()
        if not rows:
            return []

        query = np.asarray(vector, dtype="float32")
        query_norm = float(np.linalg.norm(query)) or 1.0
        scored: list[tuple[str, float]] = []
        for segment_id, raw in rows:
            try:
                candidate = np.asarray(json.loads(raw), dtype="float32")
            except Exception:
                continue
            if candidate.shape != query.shape:
                continue
            denom = (float(np.linalg.norm(candidate)) or 1.0) * query_norm
            scored.append((segment_id, float(np.dot(query, candidate) / denom)))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def delete_recording(self, recording_id: str) -> None:
        self.session.execute(
            SegmentEmbedding.__table__.delete().where(
                SegmentEmbedding.recording_id == recording_id
            )
        )

    def count(self) -> int:
        return int(
            self.session.execute(select(func.count(SegmentEmbedding.segment_id))).scalar_one()
        )

    def health(self) -> tuple[bool, str]:
        return True, f"local cosine index ({self.count()} vectors)"


class QdrantVectorStore(VectorStore):
    name = "qdrant"
    is_remote = True

    def __init__(self, url: str, collection: str, api_key: str | None, dim: int):
        self.url = url
        self.collection = collection
        self.api_key = api_key or None
        self.dim = dim
        self._client = None

    def _get_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self.url, api_key=self.api_key, timeout=10)
        return self._client

    def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self._get_client()
        if not client.collection_exists(self.collection):
            client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def upsert(self, items: list[VectorItem]) -> None:
        from qdrant_client.models import PointStruct

        if not items:
            return
        self.ensure_collection()
        self._get_client().upsert(
            collection_name=self.collection,
            points=[
                PointStruct(id=item.segment_id, vector=item.vector, payload=item.payload)
                for item in items
            ],
            wait=True,
        )

    def search(
        self, vector: list[float], limit: int, filters: SearchFilters
    ) -> list[tuple[str, float]]:
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue, Range

        must = [FieldCondition(key="variant", match=MatchValue(value=filters.variant))]
        if filters.recording_ids:
            must.append(
                FieldCondition(key="recording_id", match=MatchAny(any=filters.recording_ids))
            )
        if filters.source_types:
            must.append(FieldCondition(key="source_type", match=MatchAny(any=filters.source_types)))
        if filters.speakers:
            must.append(FieldCondition(key="speaker", match=MatchAny(any=filters.speakers)))
        if filters.review_statuses:
            must.append(
                FieldCondition(key="review_status", match=MatchAny(any=filters.review_statuses))
            )
        if filters.date_from or filters.date_to:
            must.append(
                FieldCondition(
                    key="recorded_at",
                    range=Range(
                        gte=int(filters.date_from.timestamp()) if filters.date_from else None,
                        lte=int(filters.date_to.timestamp()) if filters.date_to else None,
                    ),
                )
            )

        client = self._get_client()
        query_filter = Filter(must=must)
        try:
            response = client.query_points(
                collection_name=self.collection,
                query=vector,
                limit=limit,
                query_filter=query_filter,
                with_payload=False,
            )
            points = response.points
        except AttributeError:  # pragma: no cover - older client
            points = client.search(
                collection_name=self.collection,
                query_vector=vector,
                limit=limit,
                query_filter=query_filter,
            )
        return [(str(p.id), float(p.score)) for p in points]

    def delete_recording(self, recording_id: str) -> None:
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

        try:
            self._get_client().delete(
                collection_name=self.collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[FieldCondition(key="recording_id", match=MatchValue(value=recording_id))]
                    )
                ),
            )
        except Exception:  # pragma: no cover - best effort
            logger.warning("qdrant delete failed for %s", recording_id, exc_info=True)

    def count(self) -> int:
        try:
            return int(self._get_client().count(self.collection, exact=False).count)
        except Exception:  # pragma: no cover
            return 0

    def health(self) -> tuple[bool, str]:
        try:
            self._get_client().get_collections()
            return True, f"Qdrant at {self.url} ({self.count()} points)"
        except Exception as exc:  # pragma: no cover
            return False, f"Qdrant unreachable at {self.url}: {exc}"


def get_vector_store(session: Session, *, preference: str, url: str, collection: str,
                     api_key: str, dim: int) -> VectorStore:
    preference = (preference or "auto").lower()
    if preference in {"local", "numpy"}:
        return LocalVectorStore(session)
    if not url:
        return LocalVectorStore(session)

    qdrant = QdrantVectorStore(url, collection, api_key, dim)
    ok, reason = qdrant.health()
    if ok or preference == "qdrant":
        if not ok:
            logger.warning("Qdrant configured but unhealthy: %s", reason)
        return qdrant
    return LocalVectorStore(session)
