"""Retrieval: keyword (PostgreSQL FTS / SQLite FTS5), semantic, and hybrid.

Hybrid uses Reciprocal Rank Fusion (k=60) over the two ranked lists. RRF is
chosen deliberately: it needs no score calibration between BM25-style lexical
scores and cosine similarities, which makes it robust when the semantic backend
is swapped (Qdrant today, something else tomorrow).

Both ranked lists are *capped per exhibit* before RRF sees them (see
`_exhibit_cap`). The corpus is badly imbalanced — one recording holds 59% of all
segments — and an uncapped pool lets the longest exhibit supply the entire top
10 for any generic query, so the passage that actually answers the question is
never fused in the first place. The cap is a selection rule on ranked output; it
does not touch any score, ordering, or threshold produced by retrieval.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .filters import SearchFilters
from .models import Recording, Segment, SegmentEmbedding, Transcript
from .pipeline.embed import get_embedder
from .pipeline.vectorstore import get_vector_store
from .query import apply_segment_filters
from .schemas import SearchHit
from .text_utils import extract_terms, tokenize

_IS_SQLITE = get_settings().is_sqlite
RRF_K = 60


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


# --------------------------------------------------------------------------- #
# per-exhibit candidate capping
# --------------------------------------------------------------------------- #

def _exhibit_cap(limit: int, exhibits_matched: int) -> int:
    """How many candidates one exhibit may contribute to a ranked list.

    The corpus is imbalanced by construction: a single press conference is 1368
    of 2315 segments (59%), the smallest exhibit is 48. Without a cap, a generic
    query (`police`) returns 10 of 10 hits from the press conference and the 911
    call that actually answers `someone begging for police to come quickly` never
    reaches fusion — the search silently privileges the longest recording, which
    is a correctness bug in an evidence tool, not a ranking preference.

    The allowance is the *fair share* of the requested page::

        cap = ceil(limit / exhibits_matched * share)     (share defaults to 2.0)

    Why fair share rather than a constant:

    * It scales with `limit`. A constant 3 would still let one exhibit fill a
      page of 20, and would be needlessly tight for a page of 5.
    * It scales with how many exhibits actually matched. This is what makes the
      cap *non-starving*: when only one exhibit matches (`eminent police`),
      `exhibits_matched` is 1, so the allowance is `ceil(limit * share)`, which
      exceeds `limit` — the cap can then only remove candidates that could never
      have been displayed, so that exhibit still fills the whole page. A
      near-single-exhibit query (`a mission to the moon`, 2 exhibits, one holding
      54 of 56 candidates) likewise still lets the dominant exhibit take almost
      the whole page. Starvation is impossible by construction: the allowance is
      never below each exhibit's fair share of the page.
    * `math.ceil` guarantees cap >= 1 for any matched exhibit, so a weak but
      present exhibit cannot be rounded out of existence.

    `share` (`SEARCH_EXHIBIT_CAP_SHARE`, default 2.0) scales the fair share so
    the rule can be tuned without a code change. Why 2.0 and not strict fair
    share: with `share=1.0` a query matching two exhibits gets `limit/2` each,
    which throttles `a mission to the moon` from 10/10 to 8/10 — the cap bites a
    query that was never flooding. At 2.0 the allowance reaches `limit` as soon
    as only two exhibits match, so a focused query is untouched while a genuinely
    flooded query (4-6 exhibits, where the fair share is 2 or 3) still gets
    redistributed. Measured: `police` goes from 10/10 press-conference filler to
    6 presser + 2 from the 911 call, while `a mission to the moon` (10/10),
    `a football game on the radio` (8/10) and `woman hiding from an attacker`
    (9/10) come back identical to the uncapped baseline. At `share=3.0` the
    monopoly returns, so 2.0 is inside the working range. Values below 1.0 are
    floored at 1 by the `max(1, ...)`, so the cap can never become non-positive.
    """
    exhibits_matched = max(1, exhibits_matched)
    share = max(0.0, get_settings().search_exhibit_cap_share)
    return max(1, math.ceil(limit / exhibits_matched * share))


def _exhibit_of(session: Session, segment_ids: set[str]) -> dict[str, str]:
    """Map segment id -> recording id, for the candidate ids only.

    One bounded query over the candidate set (<= `pool` ids per channel) keeps the
    cap independent of `_hydrate`, which runs later and applies filters — the cap
    must describe what retrieval *returned*, not what survived filtering, or the
    same query would get a different cap depending on the filter chips.
    """
    if not segment_ids:
        return {}
    rows = session.execute(
        select(Segment.id, Segment.recording_id).where(Segment.id.in_(segment_ids))
    ).all()
    return {str(segment_id): str(recording_id) for segment_id, recording_id in rows}


def _cap_ranked(
    ranked: list[tuple[str, float]],
    exhibit_of: dict[str, str],
    cap: int,
) -> tuple[list[tuple[int, str, float]], list[tuple[int, str, float]]]:
    """Keep each exhibit's best `cap` candidates, preserving retrieval order.

    A first-N-per-exhibit walk down the list *as retrieval ranked it*, so an
    exhibit keeps its best-ranked candidates and only its own tail is dropped —
    never an arbitrary subset, and never a re-ordering. Nothing about the
    scorer's output changes; this is purely a selection rule over it.

    Each item carries its **original** rank, and fusion uses that rank rather
    than an index into the capped list. A capped list must not silently promote
    a survivor: with `police`, the 911 call's first passage sits at keyword rank
    5, and renumbering would score it as rank 0 — a boost the scorer never gave
    it. Keeping the original rank means a candidate's RRF contribution is
    exactly what retrieval earned it, so capping can only ever *remove* the
    press conference's tail, never inflate anything else.

    Returns `(admitted, dropped)`, both in retrieval order.
    """
    admitted: list[tuple[int, str, float]] = []
    dropped: list[tuple[int, str, float]] = []
    used: Counter = Counter()
    for rank, (segment_id, score) in enumerate(ranked):
        # A candidate whose exhibit cannot be resolved is bucketed under a
        # sentinel so it shares one allowance rather than vanishing.
        exhibit = exhibit_of.get(segment_id, "?")
        if used[exhibit] >= cap:
            dropped.append((rank, segment_id, score))
            continue
        used[exhibit] += 1
        admitted.append((rank, segment_id, score))
    return admitted, dropped


def _fuse(
    lists: dict[str, list[tuple[int, str, float]]],
    mode: str,
    into: dict[str, Candidate] | None = None,
) -> dict[str, Candidate]:
    """Combine ranked lists into candidates, using each item's original rank.

    Single-mode search uses the channel's own score, so `keyword` and `semantic`
    keep the semantics their scorers define. Hybrid/all adds `1/(K + rank + 1)`
    per channel, which is the RRF this module has always used — the cap does not
    alter `RRF_K` or the formula.
    """
    merged: dict[str, Candidate] = {} if into is None else into
    for channel, ranked in lists.items():
        for rank, segment_id, score in ranked:
            cand = merged.setdefault(segment_id, Candidate(segment_id))
            if channel == "keyword":
                cand.keyword_score = score
            else:
                cand.semantic_score = score
            cand.matched_by.add(channel)
            if mode == channel:
                cand.score = score
            elif mode in {"hybrid", "all"}:
                cand.score += 1.0 / (RRF_K + rank + 1)
    return merged


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

def _semantic_cutoff(
    settings, scanned: int, is_semantic: bool
) -> tuple[float, str]:
    """Cosine cutoff below which a semantic candidate is not a match.

    ``scanned`` is how many vectors the store ranked, **not** how many it
    returned: the ranking is a max over everything it scanned, and the top-N
    truncation happens afterwards. Passing the returned count instead would make
    the cutoff depend on the page size the caller asked for, which is not what
    the score distribution depends on.

    Returns ``(cutoff, reason)``; ``reason`` goes into diagnostics so an operator
    can see *which* rule produced an empty channel.

    Three cases, in order:

    1. **Lexical fallback.** ``hashing-lexical-v2`` is not a semantic model and
       its cosines are not comparable to a real model's. Measured on the
       42-passage test fixture, max cosine per query::

                                hashing   real model
           genuine  small       0.0233      0.2582
           nonsense qwertyuiop  0.0342      0.1500
           nonsense asdfghjkl   0.0000      0.0888
           nonsense helicopter  0.0643      0.1396

       The scales *invert*: under hashing nonsense outranks signal (0.0342 >
       0.0233), while the real model orders them correctly with a +0.108 margin.
       So no single threshold can serve both — any floor low enough to admit
       ``small`` under hashing (<= 0.0233) admits everything, and any floor high
       enough to reject ``qwertyuiop`` under hashing (> 0.0342) rejects the
       genuine passage. The fallback therefore keeps its own permissive floor,
       which only discards near-orthogonal vectors.

    2. **Reference-size corpus or larger.** Apply the configured floor verbatim.
       This is the population the number was measured on, so this is where it is
       trustworthy.

    3. **Smaller corpus.** Max-cosine-over-corpus is an extreme order statistic
       and grows with corpus size: over random samples of the demo corpus the
       mean max-cosine *of nonsense queries alone* is 0.158 at N=3, 0.195 at
       N=10, 0.241 at N=50 and 0.296 at N=500, purely because a bigger corpus
       gets more chances to contain a spurious near-miss. A floor calibrated at
       N~1000 therefore starves small corpora: max cosine over a *three-line*
       fixture for the genuinely related ``small`` -> "much smaller" tops out at
       0.2882, while max cosine over the *1865-segment live corpus* is 0.5002.
       Both figures are maxima over a corpus, not a pair of like-for-like
       passage scores (that same passage on the live corpus scores 0.2582
       against its best neighbour), so they bound the effect rather than
       quantify it passage-for-passage. A flat 0.40 sits between them and would
       discard the correct answer on the small corpus. Scaling by the same log
       ratio makes the threshold mean "how extreme is this score for a corpus
       this size" instead of "how large is this cosine in absolute terms".
    """
    if not is_semantic:
        return max(0.0, settings.lexical_min_score), "lexical-fallback"

    floor = max(0.0, settings.semantic_min_score)
    reference = max(2, settings.semantic_floor_reference_pool)
    if scanned >= reference:
        return floor, "absolute"

    # log10(n+1)/log10(reference+1): 0 at n=0, 1 at n=reference. Monotonic and
    # gentle, which suits an effect that is itself gentle.
    scale = math.log10(max(scanned, 0) + 1) / math.log10(reference + 1)
    return floor * scale, "scaled"


def _scanned(store, session: Session) -> int:
    """How many vectors the store ranks per query.

    The cosine top score is a maximum over everything the store scans, so that
    count — not the size of the returned page — is what decides how high the
    best-of-N noise floor sits. The local store scans every embedded segment in
    the (filtered) corpus; a remote store that maintains its own index reports
    its own size, and its `count()` already swallows its own transport errors and
    returns 0. A non-positive answer is therefore treated as "this store cannot
    tell me" and replaced by the local embedding count, because 0 would scale the
    cutoff to zero and disable the floor entirely — the opposite of failing safe.
    """
    reported = int(store.count())
    if reported > 0:
        return reported
    return int(
        session.execute(select(func.count(SegmentEmbedding.segment_id))).scalar_one()
    )


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
    # would see ten irrelevant passages for a query that matches nothing. The
    # floor is what makes "nothing matched" a reachable answer.
    #
    # The relative floor that used to sit here (`top * 0.5`) is gone. It was
    # introduced to trim the tail, but measurement showed it was doing almost all
    # of the work while achieving none of the goal: for `asdfghjkl` it computed
    # 0.354 * 0.5 = 0.177, which is *below* the entire noise band, so all 75
    # candidates survived and hybrid returned 25 rows. Meanwhile for a genuine
    # query topping at 0.46 it cut everything under 0.23 — discarding a large
    # part of the legitimate tail for no precision benefit. It is also
    # structurally unable to reject noise, because a relative floor is defined as
    # a fraction of the top score: when every candidate is noise, the top score
    # is noise too, and the fraction of it is still noise. Rejecting "no match at
    # all" requires an absolute threshold, which is what the configured floor
    # now is. Keeping both would mean two thresholds whose interaction has to be
    # reasoned about; one that is calibrated is better than one calibrated and
    # one arbitrary.
    cutoff, reason = _semantic_cutoff(settings, _scanned(store, session), embedder.is_semantic)
    kept = [(segment_id, score) for segment_id, score in results if score >= cutoff]
    diagnostics = {
        "embedder": embedder.name,
        "semantic": embedder.is_semantic,
        "vector_store": store.name,
        "candidates": len(results),
        "kept_above_floor": len(kept),
        "min_score": round(cutoff, 4),
        "floor_reason": reason,
        "top_score": round(results[0][1], 4) if results else 0.0,
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

    pool = max(limit * 3, 50)

    # 1. Retrieve. Scores and orderings are untouched, exactly as each scorer
    #    produced them.
    raw: dict[str, list[tuple[str, float]]] = {}
    if mode in {"keyword", "hybrid", "all"}:
        raw["keyword"] = keyword_search(session, query, filters, pool)
        diagnostics["keyword_candidates"] = len(raw["keyword"])
    if mode in {"semantic", "hybrid", "all"}:
        raw["semantic"], semantic_diag = semantic_search(session, query, filters, pool)
        diagnostics.update(semantic_diag)
        diagnostics["semantic_candidates"] = len(raw["semantic"])

    # Every channel came back empty: report "no match" and stop. This early
    # return is what keeps the cap and the anti-starvation backfill from
    # manufacturing a page out of nothing — with no candidates there is nothing
    # to cap, nothing to drop, and nothing in the overflow tail to backfill from,
    # so a nonsense query is an honest empty result rather than a quota filled
    # with the least-bad fragments.
    if not raw or not any(raw.values()):
        diagnostics.setdefault("exhibit_cap_backfilled", 0)
        diagnostics["total_candidates"] = 0
        return [], diagnostics

    # 2. Cap each ranked list *before* fusion. The cap is computed once over the
    #    union of both lists, so one rule governs both channels; applying it to
    #    only one would let the uncapped list dominate through the other and the
    #    bias would simply survive the fix.
    all_ids = {segment_id for ranked in raw.values() for segment_id, _ in ranked}
    exhibit_of = _exhibit_of(session, all_ids)
    exhibits_matched = len({exhibit_of.get(segment_id, "?") for segment_id in all_ids})
    cap = _exhibit_cap(limit, exhibits_matched)

    admitted: dict[str, list[tuple[int, str, float]]] = {}
    dropped: dict[str, list[tuple[int, str, float]]] = {}
    for channel, ranked in raw.items():
        admitted[channel], dropped[channel] = _cap_ranked(ranked, exhibit_of, cap)

    diagnostics["exhibit_cap"] = cap
    diagnostics["exhibit_cap_exhibits_matched"] = exhibits_matched
    diagnostics["exhibit_cap_candidates"] = {
        channel: len(items) for channel, items in admitted.items()
    }
    diagnostics["exhibit_cap_dropped"] = {
        channel: len(items) for channel, items in dropped.items()
    }
    diagnostics["exhibit_cap_dropped_total"] = sum(len(items) for items in dropped.values())
    admitted_per_exhibit: Counter = Counter(
        exhibit_of.get(segment_id, "?")
        for ranked in admitted.values()
        for _, segment_id, _ in ranked
    )
    diagnostics["exhibit_cap_per_exhibit"] = dict(
        admitted_per_exhibit.most_common()
    )

    # 3. Fuse the capped lists.
    candidates = _fuse(admitted, mode)

    terms = extract_terms(query)
    rows = _hydrate(session, list(candidates), filters)

    def build(segment_id: str) -> SearchHit | None:
        row = rows.get(segment_id)
        if row is None:  # filtered out after retrieval
            return None
        segment, recording, transcript = row
        cand = candidates[segment_id]
        return SearchHit(
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

    ordered = sorted(candidates, key=lambda sid: candidates[sid].score, reverse=True)
    hits = [hit for hit in (build(sid) for sid in ordered) if hit is not None]

    # 4. Anti-starvation backfill. The cap reserves each exhibit's fair share of
    #    the page; if that leaves the page short (a query only one exhibit can
    #    answer, where the cap is `limit` and inert, or an exhibit with fewer
    #    passages than its allowance), fill the remaining slots from the capped
    #    tails by the RRF score they would have earned. The cap therefore
    #    redistributes slots *between* exhibits but can never shrink the result
    #    set below what fusion produced. Backfilled hits score strictly below
    #    every admitted hit, because their original rank is worse.
    if len(hits) < limit:
        present = {hit.segment_id for hit in hits}
        overflow: dict[str, Candidate] = _fuse(dropped, mode)
        overflow_order = sorted(
            (sid for sid in overflow if sid not in present),
            key=lambda sid: overflow[sid].score,
            reverse=True,
        )
        # Backfilled candidates are not in `candidates`, so they were not
        # hydrated above. Hydrate the head of the tail — enough to fill the page
        # plus slack for rows that filters then reject.
        head = overflow_order[: (limit - len(hits)) * 2]
        if head:
            rows.update(_hydrate(session, head, filters))
        backfilled = 0
        for segment_id in overflow_order:
            if len(hits) >= limit:
                break
            candidates.setdefault(segment_id, overflow[segment_id])
            hit = build(segment_id)
            if hit is None:
                continue
            hits.append(hit)
            backfilled += 1
        diagnostics["exhibit_cap_backfilled"] = backfilled

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
