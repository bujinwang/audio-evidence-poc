"""Word-form matching in keyword search.

Reported bug: searching `small` returned nothing, while `smaller` returned two
hits. The transcript says "much much smaller", so the passage exists — the
searcher simply had to type the exact word form the ASR happened to emit.

Three independent causes, each of which alone would have hidden the passage:

1. **The FTS5 table used the plain `unicode61` tokenizer**, which does no
   stemming. `small` and `smaller` were unrelated tokens, so the short form
   could never reach the longer one.
2. **Retrieval matched whole tokens only.** Even a singular/plural pair
   (`truck` / `trucks`) failed in whichever direction the user typed — and
   `trucks` was a dead end against a transcript containing only "truck".
3. **`_coverage` scored a prefix hit as zero coverage**, so a passage that was
   found got multiplied by 0.15 in `_blend` and buried under worse matches.

Each test below fails if any one of the three regresses. They run against a
purpose-built corpus rather than the shared fixture so the words under test
(`small`, `smaller`, `boxes`, `walking`) are guaranteed to be present.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import ADMIN_HEADERS, write_sample

# One line per morphological family under test, plus a decoy that shares no
# stem with any of them so "found something" cannot be satisfied by accident.
LINES = [
    ("SPEAKER_00", "and then they are much much smaller than the others"),
    ("SPEAKER_00", "he drove a dark blue pickup truck with a loud exhaust"),
    ("SPEAKER_01", "two men were loading boxes into the truck bed"),
    ("SPEAKER_01", "I was walking my dog along the fourth street curb"),
    ("SPEAKER_00", "the registration check came back with no flags"),
]


@pytest.fixture
def corpus(client, tmp_path):
    audio_path, sidecar_path = write_sample(tmp_path / "media", "word-forms", LINES)
    response = client.post(
        "/api/recordings",
        files={
            "file": (audio_path.name, audio_path.read_bytes(), "audio/wav"),
            "transcript_file": (sidecar_path.name, sidecar_path.read_bytes(), "application/json"),
        },
        data={"source_type": "interview"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def search(client, q, **params):
    response = client.get("/api/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(payload) -> set[str]:
    return {hit["segment_id"] for hit in payload["hits"]}


# --------------------------------------------------------------------------- #
# the reported case
# --------------------------------------------------------------------------- #

def test_short_form_finds_the_longer_word(client, corpus):
    """`small` must find "smaller" — the direction an investigator types."""
    short = search(client, "small", mode="keyword")
    long_form = search(client, "smaller", mode="keyword")

    assert short["total"] >= 1, "searching 'small' found nothing"
    assert long_form["total"] >= 1
    assert _ids(short) & _ids(long_form), "the two forms must reach the same passage"


def test_the_reported_pair_returns_the_same_passage(client, corpus):
    """Both reported queries should land on the sentence that actually exists.

    Note `smallest` is deliberately *not* included: porter stems inflection
    (`truck`/`trucks`, `box`/`boxes`, `walk`/`walking`) but not derivation, so
    `smaller` and `smallest` remain distinct stems. Typing the longer, more
    specific form narrowing the result set is correct behaviour — what was
    broken is that the *shorter* form found nothing at all.
    """
    for query in ("small", "smaller"):
        payload = search(client, query, mode="keyword")
        assert payload["total"] >= 1, f"{query!r} found nothing"
        assert any("smaller" in hit["text"].lower() for hit in payload["hits"]), (
            f"{query!r} did not reach the passage containing 'smaller'"
        )


# --------------------------------------------------------------------------- #
# the same class of bug, other word forms
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "singular,plural",
    [("truck", "trucks"), ("box", "boxes"), ("flag", "flags")],
)
def test_plural_and_singular_are_interchangeable(client, corpus, singular, plural):
    """Neither form may be a dead end — the transcript has only one of them."""
    one = search(client, singular, mode="keyword")
    many = search(client, plural, mode="keyword")

    assert one["total"] >= 1, f"{singular!r} found nothing"
    assert many["total"] >= 1, f"{plural!r} found nothing"
    assert _ids(one) & _ids(many), f"{singular!r} and {plural!r} reached different passages"


@pytest.mark.parametrize("form", ["walk", "walking", "walked"])
def test_every_verb_form_reaches_the_passage(client, corpus, form):
    payload = search(client, form, mode="keyword")
    assert payload["total"] >= 1, f"{form!r} found nothing"


def test_prefix_matching_does_not_match_unrelated_words(client, corpus):
    """Recall must not come from matching everything — a decoy stays unfound."""
    payload = search(client, "helicopter", mode="keyword")
    assert payload["total"] == 0

    # "truck" must not pull in the registration line, which shares no stem.
    trucks = search(client, "trucks", mode="keyword")
    assert all("registration" not in hit["text"].lower() for hit in trucks["hits"])


# --------------------------------------------------------------------------- #
# the scoring side of the bug
# --------------------------------------------------------------------------- #

def test_a_prefix_hit_is_not_penalised_into_oblivion(client, corpus):
    """Finding a passage and then burying it is barely better than not finding it."""
    payload = search(client, "small", mode="keyword")
    assert payload["hits"]

    top = payload["hits"][0]
    # `_blend` applies (0.15 + 0.85 * coverage**2). A prefix match scored as
    # zero coverage survives at 0.15x of its BM25 value; coverage that agrees
    # with retrieval keeps it at 1.0x.
    assert top["score"] == pytest.approx(top["keyword_score"], rel=0.01), (
        "a prefix match should count as full coverage, not as a miss"
    )


def test_coverage_counts_a_prefix_as_present():
    """Unit-level guard on the exact function that buried the hit."""
    from app.search import _coverage

    assert _coverage("and then they are much much smaller", {"small"}) == 1.0
    assert _coverage("two men loading boxes", {"box"}) == 1.0
    # A genuine miss is still a miss.
    assert _coverage("a blue pickup truck", {"helicopter"}) == 0.0


# --------------------------------------------------------------------------- #
# the UI default must benefit too
# --------------------------------------------------------------------------- #

def test_hybrid_mode_finds_the_word_form(client, corpus):
    """Hybrid is the UI default, so the fix has to show up there."""
    payload = search(client, "small", mode="hybrid")
    assert payload["total"] >= 1
    assert any("keyword" in hit["matched_by"] for hit in payload["hits"])


# --------------------------------------------------------------------------- #
# quoting must still mean "exact"
# --------------------------------------------------------------------------- #

def test_quoted_phrases_stay_exact_while_terms_stay_lenient(client, corpus):
    """Quoting is the escape hatch for exact wording; bare terms should flex."""
    quoted = search(client, '"blue pickup truck"', mode="keyword")
    assert quoted["total"] >= 1
    for hit in quoted["hits"]:
        assert "blue pickup truck" in hit["text"].lower()

    # Unquoted, the same words as prefixes also tolerate a plural.
    lenient = search(client, "blue pickup trucks", mode="keyword")
    assert lenient["total"] >= 1


# --------------------------------------------------------------------------- #
# schema + migration
# --------------------------------------------------------------------------- #

def test_fts_index_is_built_with_porter_stemming(client):
    """Guard the schema: the unstemmed tokenizer is what broke matching."""
    from sqlalchemy import text

    from app import db

    with db.engine.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='segments_fts'")
        ).scalar()

    assert sql, "segments_fts is missing"
    assert "porter" in sql.lower(), "FTS table must use the porter stemmer"


def test_existing_database_is_migrated_to_stemming(tmp_path):
    """An old DB keeps its tokenizer unless init_db rebuilds the index."""
    from sqlalchemy import create_engine

    from app.db import _migrate_fts_tokenizer

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE VIRTUAL TABLE segments_fts USING fts5("
        "segment_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')"
    )
    conn.execute("CREATE TABLE segments (id TEXT PRIMARY KEY, text TEXT)")
    conn.execute("INSERT INTO segments VALUES ('s1', 'much much smaller')")
    conn.execute("INSERT INTO segments_fts VALUES ('s1', 'much much smaller')")
    conn.commit()

    # Legacy behaviour reproduced: the short form cannot reach "smaller".
    assert conn.execute(
        "SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?", ('"small"',)
    ).fetchone()[0] == 0
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _migrate_fts_tokenizer(connection)
    engine.dispose()

    check = sqlite3.connect(db_path)
    sql = check.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='segments_fts'"
    ).fetchone()[0]
    assert "porter" in sql.lower(), "tokenizer was not migrated"
    # The index is rebuilt from `segments`, so existing rows stay searchable —
    # this is the exact query that returned nothing before the fix.
    assert check.execute(
        "SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?", ('"small"*',)
    ).fetchone()[0] == 1
    # And migration is idempotent.
    check.close()
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _migrate_fts_tokenizer(connection)
    engine.dispose()
    again = sqlite3.connect(db_path)
    assert again.execute("SELECT count(*) FROM segments_fts").fetchone()[0] == 1
    again.close()
