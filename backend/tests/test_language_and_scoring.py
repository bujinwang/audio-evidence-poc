"""Language handling and scoring helpers.

Two things are pinned here because both caused real, silent failures:

1. **Language must not be left to autodetection on evidence audio.** On AMI
   distant-microphone recordings Whisper picked Welsh at p=0.73 and emitted
   gibberish; the WER on that clip fell from 77.0% to 40.4% once English was
   forced. A transcript in the wrong language is worse than no transcript — it
   looks like a real result.

2. **The evaluation normalisation must not silently change what it measures.**
   Fillers and spelled-out acronyms are a *scoring convention*, so they sit
   behind explicit flags rather than in the default path.
"""

from __future__ import annotations

from app.pipeline.transcribe import (
    FasterWhisperTranscriber,
    WhisperXTranscriber,
    _asr_language,
    resolve_transcriber,
)

from scripts.evaluate_asr import (
    char_error_rate,
    collapse_acronyms,
    term_recall,
    word_error_rate,
)


# --------------------------------------------------------------------------- #
# language resolution
# --------------------------------------------------------------------------- #

def test_auto_maps_to_none_so_the_model_decides():
    for value in (None, "", "auto", "AUTO", "detect", "none"):
        assert _asr_language(value) is None


def test_language_code_is_passed_through_case_insensitively():
    assert _asr_language("EN") == "en"
    assert _asr_language(" fr ") == "fr"


def test_transcribers_default_to_english():
    """The default must be a pinned language, not autodetect."""
    assert FasterWhisperTranscriber("small", "cpu", "int8").language == "en"
    assert WhisperXTranscriber("small", "cpu", "int8", False, "").language == "en"


def test_explicit_auto_reaches_the_provider_as_none():
    assert FasterWhisperTranscriber("small", "cpu", "int8", language="auto").language is None


def test_resolver_propagates_the_language_to_whichever_provider_wins():
    transcriber = resolve_transcriber(
        audio=None, preference="faster-whisper", language="es"  # type: ignore[arg-type]
    )
    assert transcriber.language == "es"


def test_settings_expose_a_pinned_default(set_env):
    from app.config import get_settings

    assert get_settings().whisper_language == "en"
    set_env(WHISPER_LANGUAGE="auto")
    assert get_settings().whisper_language == "auto"


# --------------------------------------------------------------------------- #
# scoring conventions
# --------------------------------------------------------------------------- #

def test_acronym_runs_collapse_on_both_sides():
    assert collapse_acronyms("what time's d n l g yeah") == "what time's dnlg yeah"
    # A genuine two-word phrase is left alone.
    assert collapse_acronyms("i am fine") == "i am fine"


def test_spelled_acronyms_do_not_score_as_four_substitutions():
    reference = "IT WAS D. N. L. G."
    hypothesis = "it was D N L G"
    assert word_error_rate(reference, hypothesis) == 0.0
    assert char_error_rate(reference, hypothesis) == 0.0


def test_two_letter_runs_are_not_treated_as_acronyms():
    # "dn lg" is two tokens, not one spelled acronym — collapsing it would hide
    # a real deletion elsewhere in the sentence.
    assert collapse_acronyms("it was dn lg") == "it was dn lg"


def test_strict_scoring_counts_fillers_and_tolerant_scoring_does_not():
    reference = "YEAH YEAH THAT WOULD BE EASY AS WELL"
    hypothesis = "that would be easy as well"
    assert word_error_rate(reference, hypothesis) > 0.0
    assert word_error_rate(reference, hypothesis, tolerant=True) == 0.0


def test_term_recall_ignores_function_words_but_catches_lost_content():
    reference = "the blue pickup truck had a cracked rear window"
    # Content words survive even though the sentence was rephrased.
    assert term_recall(reference, "pickup: a blue truck, cracked rear window") == 1.0
    # "pickup" was lost — exactly the word an investigator would search for.
    assert term_recall(reference, "the blue truck had a cracked rear window") < 1.0
