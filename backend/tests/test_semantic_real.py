"""Real (model-based) semantic search.

These tests only run when ``sentence-transformers`` is installed. The default
test environment forces the lexical ``hashing`` fallback (see conftest.py), so
the existing ``test_semantic_search_returns_ranked_passages`` pins that path.
These pin the *genuinely semantic* path: a query whose literal words are not
present in the corpus still retrieves the conceptually related passage — which
the hashing fallback cannot do (it returns ~0 cosine for unrelated word forms
and fails the score floor).

This is the behavioural contract that makes "semantic search works, with AI"
true: flipping the embedder from ``hashing-lexical-v2`` to
``sentence-transformers`` changes ``is_semantic`` from False to True and makes
concept retrieval work, not just token/prefix matching.
"""

from __future__ import annotations

import importlib.util

import pytest

from conftest import upload_ok, write_sample

_ST_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None

pytestmark = pytest.mark.skipif(
    not _ST_AVAILABLE,
    reason="sentence-transformers not installed; real semantic path untested here",
)


SEMANTIC_LINES = [
    ("SPEAKER_00", "The storage room is much smaller than we expected."),
    ("SPEAKER_01", "They arrived in a large blue truck and unloaded boxes."),
    ("SPEAKER_00", "We documented the condition of the rear window."),
]


@pytest.fixture
def semantic_recording(client, tmp_path, set_env):
    set_env(EMBEDDER="auto")  # prefer a real model whenever one is installed
    sample = write_sample(tmp_path / "media", "sem", SEMANTIC_LINES)
    return upload_ok(client, sample, source_type="interview")


def _search(client, q, **params):
    resp = client.get("/api/search", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_real_semantic_finds_concept_without_a_literal_word(client, semantic_recording):
    """`small` has no literal match in the corpus — only the `smaller` passage is
    conceptually related, and only a true semantic model retrieves it."""
    payload = _search(client, "small", mode="semantic")

    assert payload["total"] >= 1
    diag = payload["diagnostics"]
    assert diag["semantic"] is True
    assert diag["embedder"].startswith("sentence-transformers")

    top = payload["hits"][0]
    assert "smaller" in top["text"].lower()
    assert "semantic" in top["matched_by"]


def test_real_semantic_flag_is_surfaced_in_diagnostics(client, semantic_recording):
    payload = _search(client, "truck", mode="semantic")
    assert payload["diagnostics"]["semantic"] is True
    assert payload["diagnostics"]["embedder"].startswith("sentence-transformers")
    assert payload["hits"]
