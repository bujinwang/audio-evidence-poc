"""FR-04: keyword, phrase, semantic, and hybrid retrieval with filters."""

from __future__ import annotations

import pytest
from conftest import ADMIN_HEADERS, REVIEWER_HEADERS, upload_ok


@pytest.fixture
def corpus(client, witness_sample, bodycam_sample, cassette_sample):
    witness = upload_ok(client, witness_sample, source_type="interview")
    bodycam = upload_ok(client, bodycam_sample, source_type="bodycam")
    cassette = upload_ok(client, cassette_sample, source_type="cassette")
    return {"witness": witness, "bodycam": bodycam, "cassette": cassette}


def search(client, q, **params):
    response = client.get("/api/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_keyword_search_ranks_by_term_coverage(client, corpus):
    """A natural-language query should rank passages matching more of it first."""
    payload = search(client, "blue pickup truck near a community centre", mode="keyword")

    assert payload["total"] >= 2
    top_text = payload["hits"][0]["text"].lower()
    assert "blue" in top_text and "pickup" in top_text and "truck" in top_text

    # every result must share at least one content term
    for hit in payload["hits"]:
        text = hit["text"].lower()
        assert any(term in text for term in ("blue", "pickup", "truck", "community", "centre"))
    assert all(hit["keyword_score"] is not None for hit in payload["hits"])
    assert all("keyword" in hit["matched_by"] for hit in payload["hits"])


def test_quoted_phrase_search(client, corpus):
    payload = search(client, '"blue pickup truck"', mode="keyword")
    assert payload["total"] >= 1
    assert all("blue pickup truck" in hit["text"].lower() for hit in payload["hits"])


def test_keyword_search_ranks_exact_terms_above_partial_overlap(client, corpus):
    payload = search(client, "dark pickup truck", mode="keyword")
    assert payload["total"] >= 1

    top = payload["hits"][0]["text"].lower()
    assert "dark" in top and "pickup" in top
    # any passage lacking "dark" must rank below the one that has it
    for hit in payload["hits"][1:]:
        if "dark" not in hit["text"].lower():
            assert hit["score"] <= payload["hits"][0]["score"]


def test_semantic_search_returns_ranked_passages(client, corpus):
    payload = search(client, "blue pickup truck near the community centre", mode="semantic")

    assert payload["total"] > 0
    assert payload["diagnostics"]["embedder"] == "hashing-lexical-v2"
    assert payload["diagnostics"]["vector_store"] == "local-numpy"
    top = payload["hits"][0]
    assert "pickup truck" in top["text"].lower()
    assert top["semantic_score"] is not None


def test_hybrid_search_fuses_both_signals(client, corpus):
    payload = search(client, "blue pickup truck", mode="hybrid")

    assert payload["total"] > 0
    combined = {signal for hit in payload["hits"] for signal in hit["matched_by"]}
    assert "keyword" in combined
    assert "semantic" in combined
    assert payload["diagnostics"]["keyword_candidates"] > 0
    assert payload["diagnostics"]["semantic_candidates"] > 0


def test_results_carry_recording_reference_and_timestamps(client, corpus):
    payload = search(client, "pickup truck", mode="hybrid")
    assert payload["hits"]

    for hit in payload["hits"]:
        assert hit["recording_id"]
        assert hit["recording_filename"].endswith(".wav")
        assert hit["start_s"] >= 0
        assert hit["end_s"] > hit["start_s"]
        assert hit["transcript_id"]
        assert hit["review_status"] == "ai_generated"


def test_filter_by_recording(client, corpus):
    target = corpus["bodycam"]["id"]
    payload = search(client, "pickup truck", mode="hybrid", recording_id=target)

    assert payload["total"] >= 1
    assert {hit["recording_id"] for hit in payload["hits"]} == {target}


def test_filter_by_source_type_and_speaker(client, corpus):
    payload = search(client, "pickup truck", mode="hybrid", source_type="cassette")
    assert {hit["source_type"] for hit in payload["hits"]} == {"cassette"}

    speakers = search(client, "pickup truck", mode="hybrid")["hits"]
    speaker = speakers[0]["speaker"]
    filtered = search(client, "pickup truck", mode="hybrid", speaker=speaker)
    assert {hit["speaker"] for hit in filtered["hits"]} == {speaker}


def test_filter_by_review_status_tracks_corrections(client, corpus):
    payload = search(client, "silver toolbox", mode="keyword")
    segment_id = payload["hits"][0]["segment_id"]

    response = client.patch(
        f"/api/segments/{segment_id}",
        json={"text": "It had a cracked rear window and a silver tool box."},
        headers=REVIEWER_HEADERS,
    )
    assert response.status_code == 200

    corrected = search(client, "silver", mode="keyword", review_status="corrected")
    assert {hit["segment_id"] for hit in corrected["hits"]} == {segment_id}

    untouched = search(client, "silver", mode="keyword", review_status="ai_generated")
    assert segment_id not in {hit["segment_id"] for hit in untouched["hits"]}


def test_corrections_are_searchable_immediately(client, corpus):
    payload = search(client, "silver toolbox", mode="keyword")
    segment_id = payload["hits"][0]["segment_id"]

    client.patch(
        f"/api/segments/{segment_id}",
        json={"text": "It had a cracked rear window and a zippered duffel."},
        headers=REVIEWER_HEADERS,
    )

    assert search(client, "zippered duffel", mode="keyword")["total"] == 1
    assert search(client, "silver toolbox", mode="keyword")["total"] == 0


def test_empty_result_set(client, corpus):
    payload = search(client, "helicopter", mode="hybrid")
    assert payload["total"] == 0
    assert payload["hits"] == []


def test_search_options_feed_the_filter_ui(client, corpus):
    options = client.get("/api/search/options", headers=ADMIN_HEADERS).json()
    assert len(options["recordings"]) == 3
    assert set(options["source_types"]) == {"interview", "bodycam", "cassette"}
    assert "SPEAKER_01" in options["speakers"]
    assert options["review_statuses"] == ["ai_generated", "in_review", "corrected", "verified"]


