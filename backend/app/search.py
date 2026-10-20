"""Retrieval: keyword (PostgreSQL FTS / SQLite FTS5), semantic, and hybrid.

Hybrid uses Reciprocal Rank Fusion (k=60) over the two ranked lists. RRF is
chosen deliberately: it needs no score calibration between BM25-style lexical
scores and cosine similarities, which makes it robust when the semantic backend
is swapped (Qdrant today, something else tomorrow).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .filters import SearchFilters
from .models import Recording, Segment, Transcript
from .pipeline.embed import get_embedder
from .pipeline.vectorstore import get_vector_store
from .query import apply_segment_filters
from .schemas import SearchHit
from .text_utils import extract_terms, tokenize

_IS_SQLITE = get_settings().is_sqlite
RRF_K = 60
SEMANTIC_RELATIVE_FLOOR = 0.5


@dataclass
class Candidate:
    segment_id: str
    score: float = 0.0
    keyword_score: float | None = None
    semantic_score: float | None = None
    matched_by: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# query parsing
# --------------------------------------------------------------------------- #

def _split_phrases(query: str) -> tuple[list[str], list[str]]:
    phrases = [p.strip() for p in re.findall(r'"([^"]+)"', query) if p.strip()]
    remainder = re.sub(r'"[^"]+"', " ", query)
    words = extract_terms(remainder)
    return phrases, words


# --------------------------------------------------------------------------- #
# keyword retrieval
# --------------------------------------------------------------------------- #

def _fts5_expression(query: str) -> str | None:
    """OR-join the query atoms; BM25 plus term coverage does the ranking.

    Requiring every term (AND) is wrong for natural-language queries — nobody
    says "blue pickup truck near a community centre" in one breath inside one
    sentence — and falling back to OR without a precision signal lets a single
    shared word outrank a passage that matches four. So we match with OR and
    rank on coverage * BM25 (see `_coverage`).

    Terms are matched as **prefixes** (`"small"*`). Neither backend lemmatises:
    SQLite's `unicode61` tokenizer does no stemming at all, and PostgreSQL's
    `english` stemmer only conflates inflection — it maps `small`, `smaller`
    and `smallest` to three *different* lexemes. So an exact-token match meant
    searching `small` returned nothing while `smaller` returned two hits, and
    `truck`/`trucks` or `walk`/`walking` failed the same way. Prefix matching
    makes the shorter form find the longer one, which is the direction an
    investigator types. Phrases stay exact — a user who quotes a phrase is
    asking for those words in that order.
    """
    phrases, words = _split_phrases(query)
    atoms = [f'"{p}"' for p in phrases] + [f'"{w}"*' for w in words]
    if not atoms:
        return None
    return " OR ".join(atoms)


def _coverage(text_value: str, terms: set[str]) -> float:
    """Fraction of the query's content terms present in the passage.

    A term counts as present when a passage token *starts with* it, matching
    the prefix rule used for retrieval. Without this, a prefix hit would be
    scored as zero coverage and `_blend` would cut its score by 6.7x — the
    passage would be found and then buried, which is barely better than not
    finding it. Coverage must agree with whatever retrieval matched on.
    """
    if not terms:
        return 1.0
    tokens = tokenize(text_value)
    present = {term for term in terms if any(token.startswith(term) for token in tokens)}
    return len(present) / len(terms)


def _blend(raw_score: float, coverage: float) -> float:
    """Coverage dominates, BM25 breaks ties inside a coverage tier."""
    return raw_score * (0.15 + 0.85 * coverage**2)


def keyword_search(
    session: Session, query: str, filters: SearchFilters, limit: int = 50
) -> list[tuple[str, float]]:
    query = (query or "").strip()
    if not query:
        return []
    if _IS_SQLITE:
        return _keyword_sqlite(session, query, limit)
    return _keyword_postgres(session, query, limit)


def _keyword_sqlite(session: Session, query: str, limit: int) -> list[tuple[str, float]]:
    expression = _fts5_expression(query)
    if expression is None:
        return []
    sql = text(
        "SELECT segment_id, text, bm25(segments_fts) AS score FROM segments_fts "
        "WHERE segments_fts MATCH :q ORDER BY score LIMIT :limit"
    )
    try:
        rows = session.execute(sql, {"q": expression, "limit": limit}).all()
    except Exception:
        # Malformed MATCH expression (e.g. punctuation-only query).
        session.rollback()
        return []

    terms = set(extract_terms(query))
    scored = [
        (str(segment_id), _blend(-float(score), _coverage(passage or "", terms)))
        for segment_id, passage, score in rows
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [(segment_id, round(score, 6)) for segment_id, score in scored]


def _postgres_tsquery_parts(query: str) -> tuple[str, str]:
    """Split a query into a phrase fragment and a prefix-term fragment.

    PostgreSQL's `english` dictionary stems inflections (`truck`/`trucks` both
    become `truck`) but not derivations: `small`, `smaller` and `smallest` are
    three unrelated lexemes. `websearch_to_tsquery` therefore drops the shorter
    form, and searching `small` returned nothing while `smaller` returned two
    hits. `to_tsquery` with `:*` keeps the stem *and* matches continuations, so
    the shorter form finds the longer one — the direction a person searches in.

    Phrases stay exact (a quoted phrase asks for those words in that order).
    Returns raw strings to be bound as parameters — never interpolated.
    """
    phrases, words = _split_phrases(query)
    phrase_text = " OR ".join(f'"{p}"' for p in phrases)
    # `:*` is tsquery's prefix operator. Words come from `extract_terms`, which
    # yields only [a-z0-9'] — the apostrophe is not special inside a tsquery
    # term, but `to_tsquery` rejects most punctuation, so this stays narrow.
    term_text = " | ".join(f"{word}:*" for word in words)
    return phrase_text, term_text


def _keyword_postgres(session: Session, query: str, limit: int) -> list[tuple[str, float]]:
    phrase_text, term_text = _postgres_tsquery_parts(query)
    if not phrase_text and not term_text:
        return []

    # Build the boolean tsquery from whichever halves exist. The fragments are
    # bound parameters, so query content never reaches the SQL string.
    if phrase_text and term_text:
        tsquery = "websearch_to_tsquery('english', :phrases) || to_tsquery('english', :terms)"
        params = {"phrases": phrase_text, "terms": term_text, "limit": limit}
    elif phrase_text:
        tsquery = "websearch_to_tsquery('english', :phrases)"
        params = {"phrases": phrase_text, "limit": limit}
    else:
        tsquery = "to_tsquery('english', :terms)"
        params = {"terms": term_text, "limit": limit}

    sql = text(
        "SELECT s.id AS segment_id, s.text, "
        f"       ts_rank(to_tsvector('english', s.text), {tsquery}) AS score "
        "FROM segments s "
        f"WHERE to_tsvector('english', s.text) @@ ({tsquery}) "
        "ORDER BY score DESC LIMIT :limit"
    )
    rows = session.execute(sql, params).all()

    terms = set(extract_terms(query))
    scored = [
        (str(segment_id), _blend(float(score), _coverage(passage or "", terms)))
        for segment_id, passage, score in rows
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [(segment_id, round(score, 6)) for segment_id, score in scored]


# --------------------------------------------------------------------------- #
# semantic retrieval
# --------------------------------------------------------------------------- #

def semantic_search(
    session: Session, query: str, filters: SearchFilters, limit: int = 50
) -> tuple[list[tuple[str, float]], dict]:
    settings = get_settings()
    embedder = get_embedder(settings.embedder, settings.embedding_model)
    vector = embedder.embed_query(query)
    store = get_vector_store(
        session,
        preference=settings.vector_store,
        url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        dim=embedder.dim,
    )
    results = store.search(vector, limit, filters)
    # Without a floor, cosine search always returns *something* — an investigator
    # would see ten irrelevant passages for a query that matches nothing. Keep
    # the absolute floor for "no match at all" and a relative floor for the tail.
    floor = settings.semantic_min_score
    top = results[0][1] if results else 0.0
    cutoff = max(floor, top * SEMANTIC_RELATIVE_FLOOR) if top > 0 else floor
    kept = [(segment_id, score) for segment_id, score in results if score >= cutoff]
    diagnostics = {
        "embedder": embedder.name,
        "semantic": embedder.is_semantic,
        "vector_store": store.name,
        "candidates": len(results),
        "kept_above_floor": len(kept),
        "min_score": round(cutoff, 4),
    }
    return kept, diagnostics


# --------------------------------------------------------------------------- #
# fusion + hydration
# --------------------------------------------------------------------------- #

def search(
    session: Session,
    query: str,
    *,
    mode: str = "hybrid",
    filters: SearchFilters | None = None,
    limit: int = 20,
) -> tuple[list[SearchHit], dict]:
    filters = filters or SearchFilters()
    mode = (mode or "hybrid").lower()
    diagnostics: dict = {"mode": mode, "filters": filters.as_dict()}
    candidates: dict[str, Candidate] = {}

    pool = max(limit * 3, 50)

    if mode in {"keyword", "hybrid", "all"}:
        keyword_results = keyword_search(session, query, filters, pool)
        diagnostics["keyword_candidates"] = len(keyword_results)
        for rank, (segment_id, score) in enumerate(keyword_results):
            cand = candidates.setdefault(segment_id, Candidate(segment_id))
            cand.keyword_score = score
            cand.matched_by.add("keyword")
            if mode == "keyword":
                cand.score = score
            else:
                cand.score += 1.0 / (RRF_K + rank + 1)

    if mode in {"semantic", "hybrid", "all"}:
        semantic_results, semantic_diag = semantic_search(session, query, filters, pool)
        diagnostics.update(semantic_diag)
        diagnostics["semantic_candidates"] = len(semantic_results)
        for rank, (segment_id, score) in enumerate(semantic_results):
            cand = candidates.setdefault(segment_id, Candidate(segment_id))
            cand.semantic_score = score
            cand.matched_by.add("semantic")
            if mode == "semantic":
                cand.score = score
            else:
                cand.score += 1.0 / (RRF_K + rank + 1)

    if not candidates:
        return [], diagnostics

    rows = _hydrate(session, list(candidates), filters)
    terms = extract_terms(query)

    hits: list[SearchHit] = []
    for segment_id, cand in candidates.items():
        row = rows.get(segment_id)
        if row is None:  # filtered out after retrieval
            continue
        segment, recording, transcript = row
        hits.append(
            SearchHit(
                segment_id=segment.id,
                recording_id=recording.id,
                recording_filename=recording.filename,
                source_type=recording.source_type,
                recorded_at=recording.recorded_at,
                transcript_id=transcript.id,
                start_s=segment.start_s,
                end_s=segment.end_s,
                text=segment.text,
                snippet=_snippet(segment.text, terms),
                speaker=segment.speaker,
                review_status=segment.review_status,
                score=round(cand.score, 6),
                keyword_score=cand.keyword_score,
                semantic_score=cand.semantic_score,
                matched_by=sorted(cand.matched_by),
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    diagnostics["total_candidates"] = len(hits)
    return hits[:limit], diagnostics


def _hydrate(
    session: Session, segment_ids: list[str], filters: SearchFilters
) -> dict[str, tuple[Segment, Recording, Transcript]]:
    if not segment_ids:
        return {}
    stmt = (
        select(Segment, Recording, Transcript)
        .join(Recording, Recording.id == Segment.recording_id)
        .join(Transcript, Transcript.id == Segment.transcript_id)
        .where(Segment.id.in_(segment_ids))
    )
    stmt = apply_segment_filters(stmt, filters)
    return {
        segment.id: (segment, recording, transcript)
        for segment, recording, transcript in session.execute(stmt).all()
    }


def _snippet(text_value: str, terms: list[str], width: int = 260) -> str:
    if not text_value:
        return ""
    lowered = text_value.lower()
    positions = [lowered.find(term) for term in terms if term and lowered.find(term) >= 0]
    anchor = min(positions) if positions else 0
    start = max(0, anchor - 80)
    end = min(len(text_value), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text_value) else ""
    return f"{prefix}{text_value[start:end]}{suffix}"
