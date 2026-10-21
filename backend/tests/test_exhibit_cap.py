"""Per-exhibit candidate capping in retrieval.

The corpus these tests build is deliberately imbalanced, reproducing the real
one: an exhibit holding the vast majority of segments will otherwise supply the
entire top N for any generic query, so the exhibit that actually answers the
question never reaches fusion. On the production corpus a single press
conference was 1368 of 2315 segments (59%) and `police` returned 10 of 10 hits
from it while the 911 call was absent.

Both tests below fail against the uncapped implementation:

* ``test_dominant_exhibit_cannot_monopolise_the_page`` asserts the smaller
  exhibit reaches the page at all. Uncapped, the big exhibit fills every slot.
* ``test_single_exhibit_query_is_not_starved`` asserts a query only one exhibit
  can answer still returns a full page of that exhibit. A bare constant cap
  (e.g. "3 per exhibit") fails this by returning 3 hits instead of 10.

They are written against the HTTP search endpoint so they exercise the same
path the UI does: retrieval -> cap -> fusion -> hydration.
"""

from __future__ import annotations

import pytest
from conftest import ADMIN_HEADERS, write_sample

# The dominant exhibit: many passages densely repeating "harbour", the query
# term — modelled on the press conference that contains "police" hundreds of
# times. The wording and size were calibrated by measurement, not guessed:
# uncapped, this exhibit occupies the first 14 positions of the keyword ranking
# and the answering passage below never reaches the page. The repetition matters
# — with a single occurrence per line the answering passage outranked it and the
# test proved nothing.
#
# The lines also carry "commission", which no other exhibit uses. That is what
# the anti-starvation tests query: a term only one exhibit can answer, so the cap
# must not shorten that exhibit's page.
DOMINANT_LINES = [
    (
        "SPEAKER_00",
        f"The harbour harbour harbour commission noted item {n} for the harbour record today.",
    )
    for n in range(1, 21)
]

# The small exhibit that actually answers the question: a single passage that is
# the obvious match for "harbour" but ranks behind the dominant exhibit's tail.
# Modelled on the 911 call. Only one line contains "harbour" on purpose, so any
# hit from this exhibit must be the best-ranked passage of that exhibit.
ANSWER_LINES = [
    ("SPEAKER_00", "Records request number four four seven was logged."),
    ("SPEAKER_00", "Somebody is trapped near the harbour wall and the tide is coming in."),
    ("SPEAKER_00", "The caller gave no name and the line went dead."),
]

# Filler exhibits that also match the generic query. They are what makes the cap
# *engage*: the production corpus has six exhibits and `police` matched four of
# them. With only two exhibits the default share (2.0) lets the allowance reach
# `limit` and the cap is deliberately inert — that is the anti-starvation
# property, but it means a two-exhibit corpus cannot test the cap at all. These
# three bring exhibits_matched to five, matching the real flooding condition.
# Each mentions "harbour" once per exhibit so it places mid-ranking without
# displacing the dominant exhibit.
FILLER_LINES = {
    "harbour-board": [
        (
            "SPEAKER_00",
            "The board reviewed the annual harbour district budget and other matters.",
        ),
        ("SPEAKER_00", "Members agreed to reconvene next month in the usual room to continue."),
    ],
    "harbour-news": [
        (
            "SPEAKER_00",
            "A report covering the harbour and other topics of local interest was released.",
        ),
        ("SPEAKER_00", "The authority declined to comment further on the matter at this time."),
    ],
    "harbour-lecture": [
        (
            "SPEAKER_00",
            "Students visited the harbour site as part of the course requirements for the term.",
        ),
        ("SPEAKER_00", "The lesson covered tides and other coastal subjects in some detail."),
    ],
}


@pytest.fixture
def imbalanced(client, tmp_path):
    """Five exhibits: one with 20 segments, one with 3, three with 2 each."""

    def upload(audio, sidecar, source_type):
        response = client.post(
            "/api/recordings",
            files={
                "file": (audio.name, audio.read_bytes(), "audio/wav"),
                "transcript_file": (sidecar.name, sidecar.read_bytes(), "application/json"),
            },
            data={"source_type": source_type},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 201, response.text
        return response.json()

    exhibits = {}
    big_audio, big_sidecar = write_sample(tmp_path / "media", "harbour-minutes", DOMINANT_LINES)
    exhibits["big"] = upload(big_audio, big_sidecar, "other")

    small_audio, small_sidecar = write_sample(tmp_path / "media", "harbour-call", ANSWER_LINES)
    exhibits["small"] = upload(small_audio, small_sidecar, "phone_call")

    for stem, lines in FILLER_LINES.items():
        audio, sidecar = write_sample(tmp_path / "media", stem, lines)
        exhibits[stem] = upload(audio, sidecar, "speech")

    return exhibits


def search(client, q, **params):
    response = client.get("/api/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# the cap itself
# --------------------------------------------------------------------------- #

def test_dominant_exhibit_cannot_monopolise_the_page(client, imbalanced):
    """A generic term must not return a page filled from the biggest exhibit.

    Uncapped, "harbour" returns 10 of 10 from the 24-segment exhibit: BM25 has
    more chances to match there, so its tail outranks the passage that answers
    the question. The cap must let the other exhibit through.
    """
    payload = search(client, "harbour", mode="hybrid", limit=10)
    assert payload["total"] == 10

    exhibits = {hit["recording_id"] for hit in payload["hits"]}
    assert imbalanced["small"]["id"] in exhibits, (
        "the exhibit that answers the query never reached the page: "
        f"{[hit['recording_filename'] for hit in payload['hits']]}"
    )

    counts = {}
    for hit in payload["hits"]:
        counts[hit["recording_id"]] = counts.get(hit["recording_id"], 0) + 1
    assert counts[imbalanced["big"]["id"]] < 10, (
        f"one exhibit still supplied the whole page: {counts}"
    )


def test_cap_preserves_the_best_ranked_passage_of_each_exhibit(client, imbalanced):
    """The cap drops an exhibit's tail, never its head.

    The answer passage is the only small-exhibit passage matching "harbour", so
    if the cap is applied as an arbitrary subset rather than a first-N walk, it
    can be dropped even though it heads its exhibit's list.
    """
    payload = search(client, "harbour", mode="hybrid", limit=10)

    small_hits = [h for h in payload["hits"] if h["recording_id"] == imbalanced["small"]["id"]]
    assert small_hits, "the answering exhibit was dropped entirely"
    assert any("harbour wall" in hit["text"].lower() for hit in small_hits), (
        "the best-ranked passage of the answering exhibit was not the one kept"
    )


def test_single_exhibit_query_is_not_starved(client, imbalanced):
    """A query only one exhibit can answer must still return its full page.

    This is the anti-starvation guarantee. With exhibits_matched == 1 the cap
    exceeds `limit`, so it is inert. A bare per-exhibit constant would cut this
    page to that constant instead of the ten passages that exist.
    """
    payload = search(client, "commission", mode="hybrid", limit=10)

    assert payload["total"] == 10, (
        f"a query only one exhibit answers returned {payload['total']} hits, not 10"
    )
    assert {hit["recording_id"] for hit in payload["hits"]} == {imbalanced["big"]["id"]}


def test_single_exhibit_query_is_not_starved_in_each_mode(client, imbalanced):
    """The cap is inert for a mono-exhibit query regardless of how it is reached."""
    for mode in ("keyword", "semantic", "hybrid"):
        payload = search(client, "commission", mode=mode, limit=10)
        assert payload["total"] == 10, f"mode={mode} returned {payload['total']} hits"


# --------------------------------------------------------------------------- #
# the cap applies to every mode
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_cap_applies_in_every_mode_that_floods(client, imbalanced, mode):
    """Switching mode must not bring the monopoly back.

    The mode tabs invite the operator to compare algorithms; if only hybrid were
    capped, the same bias would reappear one click away. Keyword and semantic are
    both capped in `search()`; these two modes both return every matching exhibit
    in this corpus, so both are testable here.

    (`mode="semantic"` is not covered by this assertion because the lexical
    hashing embedder used in tests retrieves only the dominant exhibit for
    "harbour" — one exhibit matched, so the cap is correctly inert. The unit test
    `test_cap_and_diagnostics_are_channel_agnostic` below pins that the same cap
    is computed and applied on the semantic channel as well.)
    """
    payload = search(client, "harbour", mode=mode, limit=10)

    exhibits = {hit["recording_id"] for hit in payload["hits"]}
    assert imbalanced["small"]["id"] in exhibits, (
        f"mode={mode} produced a page from a single exhibit: "
        f"{[hit['recording_filename'] for hit in payload['hits']]}"
    )


def test_cap_and_diagnostics_are_channel_agnostic(client, imbalanced):
    """Both channels are capped by the same rule, in every mode.

    The cap is computed once over the union of the two lists and applied to each,
    so a mode that only runs one channel reports the same allowance as hybrid.
    These are the diagnostics the UI surfaces, so a regression in one channel
    must be visible.
    """
    for mode in ("keyword", "semantic", "hybrid"):
        diagnostics = search(client, "harbour", mode=mode, limit=10)["diagnostics"]
        assert diagnostics["exhibit_cap"] >= 1, mode
        assert diagnostics["exhibit_cap_exhibits_matched"] >= 1, mode
        assert diagnostics["exhibit_cap_dropped"], mode
        assert set(diagnostics["exhibit_cap_candidates"]) <= {"keyword", "semantic"}, mode

    keyword = search(client, "harbour", mode="keyword", limit=10)["diagnostics"]
    semantic = search(client, "harbour", mode="semantic", limit=10)["diagnostics"]
    assert set(keyword["exhibit_cap_dropped"]) == {"keyword"}
    assert set(semantic["exhibit_cap_dropped"]) == {"semantic"}


# --------------------------------------------------------------------------- #
# observability
# --------------------------------------------------------------------------- #

def test_diagnostics_expose_the_cap_and_the_dropped_count(client, imbalanced):
    """The cap must be observable in production, not a silent behaviour change."""
    payload = search(client, "harbour", mode="hybrid", limit=10)
    diagnostics = payload["diagnostics"]

    assert diagnostics["exhibit_cap"] >= 1
    # Every exhibit that matched is counted, including the fillers — this is the
    # number the formula divides by, so it must reflect the real fan-out.
    assert diagnostics["exhibit_cap_exhibits_matched"] == len(imbalanced)
    # Both channels are capped, and the dropped count is per channel so a
    # regression in one of them is visible.
    assert set(diagnostics["exhibit_cap_dropped"]) == {"keyword", "semantic"}
    assert diagnostics["exhibit_cap_dropped_total"] > 0
    assert diagnostics["exhibit_cap_per_exhibit"]


def test_diagnostics_report_an_allowance_above_the_page_for_a_focused_query(client, imbalanced):
    """A mono-exhibit query must report an allowance that cannot shorten the page.

    The cap still drops candidates beyond `ceil(limit * share)`, but those could
    never have been displayed — the contract that matters is that the allowance
    exceeds `limit`, which is what makes starvation impossible.
    """
    payload = search(client, "commission", mode="hybrid", limit=10)
    diagnostics = payload["diagnostics"]

    assert diagnostics["exhibit_cap_exhibits_matched"] == 1
    assert diagnostics["exhibit_cap"] > payload["total"], (
        "the allowance must exceed the page it is supposed to leave intact"
    )


# --------------------------------------------------------------------------- #
# the cap is configurable
# --------------------------------------------------------------------------- #

def test_cap_scales_with_the_requested_limit(client, imbalanced):
    """The allowance must grow with `limit`, or a large page is throttled."""
    small_page = search(client, "harbour", mode="hybrid", limit=5)
    large_page = search(client, "harbour", mode="hybrid", limit=20)

    assert large_page["diagnostics"]["exhibit_cap"] > small_page["diagnostics"]["exhibit_cap"]


def test_cap_share_is_configurable_via_settings(client, imbalanced, set_env):
    """A large share must loosen the cap back to the old monopolising behaviour.

    This doubles as the proof that the cap is what fixes the distribution: with
    the share raised until the allowance exceeds the candidate supply, the
    result reverts to exactly the uncapped page — the dominant exhibit taking
    every slot and the answering passage absent.
    """
    capped = search(client, "harbour", mode="hybrid", limit=10)
    assert imbalanced["small"]["id"] in {hit["recording_id"] for hit in capped["hits"]}

    set_env(SEARCH_EXHIBIT_CAP_SHARE="100")
    loose = search(client, "harbour", mode="hybrid", limit=10)

    assert loose["diagnostics"]["exhibit_cap"] > capped["diagnostics"]["exhibit_cap"]
    assert loose["diagnostics"]["exhibit_cap_dropped_total"] == 0, (
        "a cap above the candidate supply must drop nothing"
    )
    # The old, uncapped page: every slot from the dominant exhibit.
    assert {hit["recording_id"] for hit in loose["hits"]} == {imbalanced["big"]["id"]}
