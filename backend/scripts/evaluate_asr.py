#!/usr/bin/env python3
"""Measure transcription accuracy against the reference transcripts.

The sidecar `.transcript.json` files in the demo and noisy test sets are ground
truth: they were written from the script, with exact timings. That makes it
possible to answer the question the PRD asks for — *how good is the ASR, and
where does it break down?* — instead of guessing from a couple of spot checks.

Reports word error rate (WER), character error rate (CER), and real-time factor
(RTF) per file, so accuracy and speed can be traded off with numbers.

    python scripts/evaluate_asr.py --dir ../data/test-audio
    python scripts/evaluate_asr.py --dir ../data/test-audio --model small
    python scripts/evaluate_asr.py --dir ../data/samples --model small --json out.json

WER/CER are computed here rather than pulled from `jiwer`: it is a dozen lines of
edit distance, and the project should not grow a dependency to measure itself.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.transcribe import FasterWhisperTranscriber  # noqa: E402

PUNCT = re.compile(r"[^\w\s']", re.UNICODE)
SPACE = re.compile(r"\s+")

# Disfluencies and back-channels. Human transcripts (and Whisper, sometimes)
# write these; whether they count as errors is a scoring *convention*, not a
# fact about the audio, so they are handled by an explicit flag rather than
# baked into the default.
FILLERS = {
    "uh", "uhh", "uhm", "um", "umm", "er", "erm", "ah", "eh", "hmm", "mm",
    "mhm", "mmhmm", "uhhuh", "yeah", "yep", "yup", "okay", "ok", "oh",
}

# A run of single letters is how transcripts spell acronyms ("D. N. L. G.").
# Collapsing runs on both sides stops the *formatting* being counted as four
# substitutions. The lookarounds leave the surrounding spaces intact — consuming
# them would glue the collapsed token to the previous word (`was d n l g` ->
# `wasdnlg`), which is exactly the silent corruption this is meant to prevent.
_LETTER_RUN = re.compile(r"(?:(?<=^)|(?<=\s))((?:[a-z]\s+){2,}[a-z])(?=\s|$)")


def normalise(text: str) -> str:
    """Case-fold and strip punctuation; ASR never reproduces either reliably."""
    lowered = PUNCT.sub(" ", text.lower())
    return SPACE.sub(" ", lowered).strip()


def collapse_acronyms(text: str) -> str:
    """Join runs of single letters: ``d n l g`` -> ``dnlg``.

    AMI writes acronyms letter-by-letter with full stops; Whisper writes the
    word as heard. Scoring that as four substitutions measures punctuation
    convention, not transcription quality.
    """
    return _LETTER_RUN.sub(lambda m: m.group(1).replace(" ", ""), text)


def score_form(text: str, *, tolerant: bool) -> str:
    """The normalisation used for one side of a WER comparison."""
    cleaned = collapse_acronyms(normalise(text))
    if tolerant:
        tokens = [t for t in cleaned.split() if t not in FILLERS]
        cleaned = " ".join(tokens)
    return cleaned


# Function words carry no searchable content; WER punishes them, retrieval does not.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "as", "is", "are", "was", "were", "be", "been", "it", "its",
    "i", "you", "we", "he", "she", "they", "me", "my", "your", "our", "his",
    "her", "them", "that", "this", "these", "those", "there", "here", "so",
    "do", "does", "did", "have", "has", "had", "will", "would", "can", "could",
    "just", "like", "well", "then", "than", "what", "which", "who", "when",
    "not", "no", "up", "out", "about", "into", "from", "by", "get", "got",
}


def content_terms(text: str) -> set[str]:
    """The words in a transcript that a keyword search would actually target."""
    return {
        token
        for token in normalise(text).split()
        if len(token) > 2 and token not in STOPWORDS and token not in FILLERS
    }


def term_recall(reference: str, hypothesis: str) -> float:
    """Fraction of the reference's content words that survive anywhere in the ASR.

    This is the metric that matters for an evidence *search* tool. WER is
    sequence-aligned and counts word order; retrieval is not. A transcript can
    score 50% WER while every name, address, and case number is still present
    and findable — and it can score 15% WER while the one phrase an investigator
    needs was dropped. Reported alongside WER, never instead of it.
    """
    reference_terms = content_terms(reference)
    if not reference_terms:
        return 1.0
    return len(reference_terms & content_terms(hypothesis)) / len(reference_terms)


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    """Levenshtein distance over token lists (or character lists)."""
    if not reference:
        return len(hypothesis)
    previous = list(range(len(hypothesis) + 1))
    for i, ref_token in enumerate(reference, start=1):
        current = [i]
        for j, hyp_token in enumerate(hypothesis, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (0 if ref_token == hyp_token else 1)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str, *, tolerant: bool = False) -> float:
    ref = score_form(reference, tolerant=tolerant).split()
    hyp = score_form(hypothesis, tolerant=tolerant).split()
    return edit_distance(ref, hyp) / max(len(ref), 1)


def char_error_rate(reference: str, hypothesis: str, *, tolerant: bool = False) -> float:
    ref = list(score_form(reference, tolerant=tolerant).replace(" ", ""))
    hyp = list(score_form(hypothesis, tolerant=tolerant).replace(" ", ""))
    return edit_distance(ref, hyp) / max(len(ref), 1)


def audio_duration(path: Path) -> float:
    with wave.open(str(path)) as handle:
        return handle.getnframes() / handle.getframerate()


def load_reference(sidecar: Path) -> str:
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    segments = payload.get("segments", payload if isinstance(payload, list) else [])
    return " ".join(seg.get("text", "") for seg in segments).strip()


def find_pairs(directory: Path) -> list[tuple[Path, Path]]:
    pairs = []
    for audio in sorted(directory.glob("*.wav")):
        sidecar = audio.with_suffix(".transcript.json")
        if sidecar.exists():
            pairs.append((audio, sidecar))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="../data/test-audio",
                        help="directory of .wav + .transcript.json pairs")
    parser.add_argument("--model", default="base", help="whisper model size or path")
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--language", default="en",
                        help="ASR language code, or 'auto' to let the model detect. "
                             "Pinning is the default: autodetect picked Welsh on "
                             "AMI audio at p=0.73 and tripled the error rate.")
    parser.add_argument("--json", default=None, help="write raw results to this path")
    parser.add_argument("--show-errors", type=int, default=0, metavar="N",
                        help="print a word-level diff for the N worst files")
    parser.add_argument("--tolerant", action="store_true",
                        help="drop fillers/back-channels (uh, mm-hmm, yeah) from both "
                             "sides before scoring — reports the convention where "
                             "disfluency is not an error")
    parser.add_argument("--denoise", action="store_true",
                        help="also transcribe a DeepFilterNet-denoised copy and report the delta")
    args = parser.parse_args()

    directory = Path(args.dir).expanduser().resolve()
    pairs = find_pairs(directory)
    if not pairs:
        print(f"no .wav/.transcript.json pairs in {directory}", file=sys.stderr)
        return 2

    denoiser = None
    if args.denoise:
        from app.pipeline.denoise import DeepFilterNetDenoiser

        denoiser = DeepFilterNetDenoiser()
        ok, detail = denoiser.available()
        if not ok:
            print(f"denoising unavailable: {detail}", file=sys.stderr)
            return 2
        print(f"denoiser: {detail}\n")

    transcriber = FasterWhisperTranscriber(
        args.model, args.device, args.compute_type, language=args.language
    )
    available, detail = transcriber.available()
    if not available:
        print(f"transcription unavailable: {detail}", file=sys.stderr)
        return 2

    print(f"model: {args.model}  device: {args.device}  compute: {args.compute_type}")
    print(f"language: {args.language}  scoring: "
          f"{'tolerant (fillers ignored)' if args.tolerant else 'strict'}")
    print(f"files: {len(pairs)}  in {directory}\n")

    header = f"{'file':<40}{'dur':>7}{'WER':>8}"
    if denoiser:
        header += f"{'WERdn':>8}{'delta':>8}"
    header += f"{'CER':>8}{'termRec':>9}{'RTF':>7}{'segs':>6}"
    print(header)
    print("-" * len(header))

    results = []
    total_ref_words = 0
    total_edits = 0

    for audio, sidecar in pairs:
        duration = audio_duration(audio)
        started = time.perf_counter()
        try:
            result = transcriber.transcribe(audio)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"{audio.name[:40]:<40}{duration:>6.1f}s   FAILED: {exc}")
            results.append({"file": audio.name, "error": str(exc)})
            continue
        elapsed = time.perf_counter() - started

        reference = load_reference(sidecar)
        hypothesis = " ".join(seg["text"] for seg in result.segments)

        wer = word_error_rate(reference, hypothesis, tolerant=args.tolerant)
        cer = char_error_rate(reference, hypothesis, tolerant=args.tolerant)
        recall = term_recall(reference, hypothesis)
        rtf = elapsed / duration if duration else 0.0

        ref_words = len(score_form(reference, tolerant=args.tolerant).split())
        total_ref_words += ref_words
        total_edits += round(wer * max(ref_words, 1))

        line = f"{audio.name[:40]:<40}{duration:>6.1f}s{wer:>7.1%}"
        denoised_wer = None
        if denoiser is not None:
            with tempfile.TemporaryDirectory(prefix="eval-dn-") as tmp:
                cleaned = Path(tmp) / "denoised.flac"
                try:
                    denoiser.denoise(audio, cleaned)
                    dn_result = transcriber.transcribe(cleaned)
                    dn_text = " ".join(seg["text"] for seg in dn_result.segments)
                    denoised_wer = word_error_rate(reference, dn_text, tolerant=args.tolerant)
                    line += f"{denoised_wer:>7.1%}{denoised_wer - wer:>+7.1%}"
                except Exception as exc:  # noqa: BLE001
                    line += f"{'ERR':>8}{str(exc)[:8]:>8}"
        line += f"{cer:>7.1%}{recall:>9.1%}{rtf:>7.2f}{len(result.segments):>6}"
        print(line)

        results.append(
            {
                "file": audio.name,
                "duration_s": round(duration, 2),
                "wer": round(wer, 4),
                "cer": round(cer, 4),
                "term_recall": round(recall, 4),
                "rtf": round(rtf, 3),
                "wer_denoised": round(denoised_wer, 4) if denoised_wer is not None else None,
                "segments": len(result.segments),
                "elapsed_s": round(elapsed, 2),
                "reference": reference,
                "hypothesis": hypothesis,
                "language": result.language,
                "settings": result.settings,
            }
        )

    scored = [r for r in results if "wer" in r]
    if scored:
        corpus_wer = total_edits / max(total_ref_words, 1)
        mean_wer = sum(r["wer"] for r in scored) / len(scored)
        mean_recall = sum(r["term_recall"] for r in scored) / len(scored)
        mean_rtf = sum(r["rtf"] for r in scored) / len(scored)
        print("-" * 87)
        print(f"{'mean':<40}{'':>7}{mean_wer:>7.1%}{'':>8}{'':>9}"
              f"{mean_recall:>9.1%}{mean_rtf:>7.2f}")
        print(f"\ncorpus WER (weighted by reference length): {corpus_wer:.1%}")
        print(f"mean content-term recall: {mean_recall:.1%} "
              f"(words the ASR kept that a search would target)")

    if args.show_errors and scored:
        worst = sorted(scored, key=lambda r: r["wer"], reverse=True)[: args.show_errors]
        for result in worst:
            print(f"\n--- {result['file']}  WER {result['wer']:.1%} ---")
            reference = score_form(result["reference"], tolerant=args.tolerant).split()
            hypothesis = score_form(result["hypothesis"], tolerant=args.tolerant).split()
            matcher = difflib.SequenceMatcher(None, reference, hypothesis)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "equal":
                    continue
                print(f"  {tag:<8} REF: {' '.join(reference[i1:i2])!r:<44} "
                      f"ASR: {' '.join(hypothesis[j1:j2])!r}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"model": args.model, "results": results}, indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
