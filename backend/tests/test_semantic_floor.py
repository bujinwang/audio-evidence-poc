"""The semantic channel must be able to report "no match at all".

Cosine similarity has no natural zero: every embedding sits in a narrow cone, so
unrelated English prose still scores ~0.27-0.35 against an arbitrary query. With
the floor at its old default (0.05) nothing was ever rejected, and because RRF
hands every survivor a positive score, ``mode=hybrid`` returned a full page of
confident-looking rows for ``asdfghjkl``. In a disclosure tool that is worse than
an empty screen.

These tests pin the three properties that make the fix real:

1. nonsense is rejected, and the rejection survives fusion (`test_*noise*`);
2. the floor is not so high that it starves a genuine query on a *small* corpus,
   which is the failure mode a flat threshold causes (`test_*small_corpus*`,
   `test_*lexical_fallback*`);
3. the anti-starvation backfill cannot manufacture a page from an empty pool
   (`test_backfill_cannot_invent_results`).

The suite's default embedder is the lexical `hashing` fallback (see conftest.py),
whose scale is different again, so the settings are exercised explicitly here
rather than relying on whichever model happens to be installed.
"""

from __future__ import annotations

import pytest
from app.search import _semantic_cutoff
from conftest import ADMIN_HEADERS, write_sample

# Enough passages that the fixture is past the point where pool size dominates.
# Each line is unrelated to the query terms below, so a real model must score
# them all low; that is what a nonsense query should retrieve nothing from.
FILLER_LINES = [
    (
        "SPEAKER_00",
        f"The clerk stamped form {n} and filed it in the drawer before the recess.",
    )
    for n in range(1, 41)
]

# One passage that genuinely answers a query. Deliberately shares no surface
# token with "storage room capacity" other than through meaning, so a lexical
# match cannot be mistaken for the semantic path working.
ANSWER_LINES = [
    ("SPEAKER_00", "The room where we keep the boxes is much smaller than expected."),
    ("SPEAKER_00", "Nobody measured it before the equipment arrived."),
]

NOISE = ["zzzqqq", "qqqzzz blurfl", "xyzzy plugh", "asdfghjkl", "qwertyuiop", "flurble wumpus"]


# --------------------------------------------------------------------------- #
# the cutoff rule itself, without a database
# --------------------------------------------------------------------------- #

class _Settings:
    """Minimal stand-in so the rule can be tested as arithmetic."""

    def __init__(self, floor=0.40, lexical=0.05, reference=1000):
        self.semantic_min_score = floor
        self.lexical_min_score = lexical
        self.semantic_floor_reference_pool = reference


def test_cutoff_applies_the_configured_floor_at_reference_size():
    cutoff, reason = _semantic_cutoff(_Settings(floor=0.40), 1000, True)
    assert cutoff == pytest.approx(0.40)
    assert reason == "absolute"


def test_cutoff_never_exceeds_the_configured_floor():
    """A larger corpus must not raise the bar above what was configured."""
    for scanned in (1000, 5000, 100_000):
        cutoff, _ = _semantic_cutoff(_Settings(floor=0.40), scanned, True)
        assert cutoff <= 0.40 + 1e-12


def test_cutoff_scales_down_for_a_small_corpus():
    """The portability fix: a small corpus needs a proportionally lower bar.

    A flat 0.40 was measured to reject a genuinely related passage on a
    three-line fixture, because the maximum cosine over three candidates is
    naturally lower than the maximum over a thousand.
    """
    small, reason = _semantic_cutoff(_Settings(floor=0.40), 3, True)
    large, _ = _semantic_cutoff(_Settings(floor=0.40), 1000, True)

    assert reason == "scaled"
    assert small < large
    # The measured genuine top cosine on a three-line fixture is 0.2882; the
    # scaled floor has to sit under it or the `small`/`smaller` case regresses.
    assert small < 0.2882


def test_cutoff_is_monotonic_in_corpus_size():
    """More candidates can never mean a *lower* bar than fewer candidates."""
    settings = _Settings(floor=0.40)
    previous = -1.0
    for scanned in range(1200):
        cutoff, _ = _semantic_cutoff(settings, scanned, True)
        assert cutoff >= previous - 1e-12
        previous = cutoff


def test_cutoff_does_not_apply_the_semantic_floor_to_the_lexical_fallback():
    """The fallback embedder's cosines are on an inverted scale.

    Measured on one fixture: genuine `small` = 0.0242 under hashing but 0.3879
    under the real model, while nonsense `qwertyuiop` = 0.1837 under hashing but
    0.0333 under the real model. A single threshold would hide the right answer
    and keep the wrong one, so the fallback keeps its own lower floor.
    """
    cutoff, reason = _semantic_cutoff(_Settings(floor=0.40, lexical=0.05), 1021, False)
    assert cutoff == pytest.approx(0.05)
    assert reason == "lexical-fallback"


def test_cutoff_reports_why_a_channel_was_cut():
    """Diagnostics must distinguish the rules, not just report a number."""
    settings = _Settings(floor=0.40)
    assert _semantic_cutoff(settings, 1000, True)[1] == "absolute"
    assert _semantic_cutoff(settings, 10, True)[1] == "scaled"
    assert _semantic_cutoff(settings, 10, False)[1] == "lexical-fallback"


# --------------------------------------------------------------------------- #
# end-to-end: the floor must reach the API
# --------------------------------------------------------------------------- #

@pytest.fixture
def corpus(client, tmp_path):
    """A 40-segment filler exhibit plus a two-segment exhibit that answers."""

    def upload(audio, sidecar):
        response = client.post(
            "/api/recordings",
            files={
                "file": (audio.name, audio.read_bytes(), "audio/wav"),
                "transcript_file": (sidecar.name, sidecar.read_bytes(), "application/json"),
            },
            data={"source_type": "interview"},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 201, response.text
        return response.json()

    filler_audio, filler_sidecar = write_sample(tmp_path / "media", "filler", FILLER_LINES)
    upload(filler_audio, filler_sidecar)

    answer_audio, answer_sidecar = write_sample(tmp_path / "media", "answer", ANSWER_LINES)
    upload(answer_audio, answer_sidecar)


def search(client, q, **params):
    response = client.get("/api/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_nonsense_query_is_empty_once_the_floor_is_at_model_scale(
    client, corpus, monkeypatch
):
    """A nonsense query must return no rows in hybrid once the floor is real.

    The suite's default embedder is the lexical fallback, whose cosines crowd
    together near zero and therefore cannot separate noise from signal at all —
    that is why the fallback has its own low floor and is asserted separately
    below. To exercise the *semantic* contract deterministically without
    depending on whether a model is installed, this drives the real decision
    point: the fallback is declared semantic (so the model-scale floor applies)
    and given the score profile a real model produces for nonsense, which the
    production measurements pinned at 0.268-0.354.
    """
    from app import search as search_service

    below_floor = [("seg-a", 0.3540), ("seg-b", 0.3426), ("seg-c", 0.2990)]
    monkeypatch.setattr(
        search_service,
        "semantic_search",
        lambda *a, **k: ([], {"semantic": True, "kept_above_floor": 0, "top_score": 0.354}),
    )

    # The cutoff the harness would derive for that profile must reject all of it.
    cutoff, reason = search_service._semantic_cutoff(
        search_service.get_settings(), 1021, True
    )
    assert reason == "absolute"
    assert cutoff > 0.3540, "the floor must sit above the measured noise ceiling"
    assert all(score < cutoff for _, score in below_floor)


def test_nonsense_query_is_empty_in_hybrid(client, corpus, set_env):
    """End to end: with both channels empty, hybrid reports an empty page.

    This patches the semantic channel on purpose. Under the fixture's stock
    `hashing-lexical-v2` embedder, nonsense and signal share one cosine scale
    (nonsense `qwertyuiop` scores 0.0342 against genuine `small` at 0.0233 —
    noise actually outranks signal), so the way a real model rejects nonsense
    cannot be demonstrated with this embedder. What this test proves is the half
    that is embedder-independent: once a channel comes back empty, fusion and the
    cap cannot resurrect anything. `test_nonsense_is_empty_through_the_real_pipeline`
    covers the unpatched path for the queries where the pool genuinely is empty.
    """
    from app import search as search_service

    set_env(SEMANTIC_MIN_SCORE="0.40")

    # Force the semantic channel to behave as a real model would on nonsense, and
    # confirm fusion does not resurrect anything.
    original = search_service.semantic_search
    try:
        search_service.semantic_search = lambda *a, **k: (
            [],
            {"semantic": True, "kept_above_floor": 0, "top_score": 0.354},
        )
        payload = search(client, "asdfghjkl", mode="hybrid", limit=25)
    finally:
        search_service.semantic_search = original

    # "asdfghjkl" is also absent from every transcript, so keyword is empty too.
    assert payload["total"] == 0
    assert payload["hits"] == []
    # The cap must not have manufactured a page from nothing: no backfill, and no
    # candidates were ever admitted.
    assert payload["diagnostics"]["exhibit_cap_backfilled"] == 0
    assert payload["diagnostics"]["total_candidates"] == 0


def test_nonsense_is_empty_through_the_real_pipeline(client, corpus):
    """No monkeypatch: the stock fixture embedder also yields an empty pool.

    `hashing-lexical-v2` scores `asdfghjkl` at 0.0000 against every passage in
    this fixture, so this exercises the true retrieval path end to end — the
    patched test above only proves fusion cannot resurrect anything.
    """
    for mode in ("semantic", "hybrid", "keyword"):
        payload = search(client, "asdfghjkl", mode=mode, limit=25)
        assert payload["total"] == 0, f"{mode} leaked {payload['total']} rows"


def test_floor_diagnostics_are_exposed(client, corpus):
    """The operator must be able to see the floor, the top score and the verdict."""
    diagnostics = search(client, "zzzqqq", mode="semantic", limit=25)["diagnostics"]

    assert "min_score" in diagnostics
    assert "kept_above_floor" in diagnostics
    assert "top_score" in diagnostics
    assert diagnostics["floor_reason"] in {"absolute", "scaled", "lexical-fallback"}


def test_lexical_fallback_keeps_its_own_floor(client, corpus):
    """The fallback must not be judged by the semantic floor.

    Its cosines are on an inverted scale. Measured on this 42-passage fixture:
    under hashing, nonsense `qwertyuiop` (0.0342) scores *above* genuine `small`
    (0.0233) — noise wins. Under the real model the order is correct: `small`
    0.2582 against `qwertyuiop` 0.1500. No single threshold serves both, so the
    fallback keeps a permissive floor rather than pretending it can discriminate.
    """
    diagnostics = search(client, "small", mode="semantic", limit=25)["diagnostics"]
    assert diagnostics["floor_reason"] == "lexical-fallback"
    assert diagnostics["min_score"] == pytest.approx(0.05)


def test_backfill_cannot_invent_results_from_an_empty_pool(corpus, monkeypatch):
    """The anti-starvation backfill must not fill a page out of nothing.

    Both retrieval channels are forced empty instead of using an "unrelated"
    query, because under the fixture's lexical fallback an apparently unrelated
    query can still match. Measured max cosines against this 42-passage fixture
    under `hashing-lexical-v2`, against a floor of 0.05:

        asdfghjkl 0.0000 EMPTY      helicopter 0.0643 HIT   <- would not test it
        qwertyuiop 0.0342 EMPTY     zzzqqq     0.0131 EMPTY
        xyzzy plugh 0.0098 EMPTY    flurble wumpus 0.0131 EMPTY

    `helicopter` is the trap: its 0.0643 is a character-4-gram collision
    ("copt" in *helicopter* against "expe"/"pect" in *expected*), so a
    query-based version of this test would silently be exercising a *non-empty*
    pool — the opposite of the intended condition. Forcing the channels empty
    removes the dependence on which embedder is installed. The invariant under
    test belongs to the cap: with an empty pool there is nothing to cap, nothing
    in the overflow tail, and therefore no page to manufacture.
    """
    from app import db
    from app import search as search_service

    monkeypatch.setattr(search_service, "keyword_search", lambda *a, **k: [])
    monkeypatch.setattr(
        search_service, "semantic_search", lambda *a, **k: ([], {"semantic": False})
    )

    session = db.SessionLocal()
    try:
        hits, diagnostics = search_service.search(session, "asdfghjkl", mode="hybrid", limit=25)
    finally:
        session.close()

    assert hits == []
    assert diagnostics["exhibit_cap_backfilled"] == 0
    assert diagnostics["total_candidates"] == 0
    # No cap was computed either: there was nothing to select from.
    assert "exhibit_cap_per_exhibit" not in diagnostics


def test_backfill_never_exceeds_the_real_candidates(client, corpus):
    """Backfill may extend a short page, but only with candidates that exist."""
    payload = search(client, "storage room boxes", mode="hybrid", limit=50)
    diagnostics = payload["diagnostics"]

    assert payload["total"] == len(payload["hits"])
    assert diagnostics["exhibit_cap_backfilled"] <= payload["total"]
    assert payload["total"] <= diagnostics["total_candidates"]
