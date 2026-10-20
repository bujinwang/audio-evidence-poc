#!/usr/bin/env python3
"""Decompose the real-audio error rate into its causes.

The headline WER on the AMI clips is ~55%, which is alarming next to the ~4% the
system scores on its own synthetic sets. The honest question is *why*, and the
answer decides what the product has to do about it.

AMI publishes the same meetings through two microphone conditions with identical
human transcripts and identical segment boundaries:

* ``ihm`` — each participant's headset mic, near-field and speaker-isolated;
* ``sdm`` — one distant table microphone picking up the whole room.

That gives a controlled experiment. Holding the *text* fixed and varying the
capture, the difference in WER is microphone and overlap, not language modelling.

Two more conditions come from how a clip is assembled. Scoring a whole
reconstructed clip forces the ASR to hear four people talking at once through one
stream; scoring each utterance as its own short file removes the overlap entirely.
Conversations in these windows overlap ~90% of the time, so this is not a detail.

    per-turn, headset      <- what the ASR can do on clean, non-overlapping speech
    per-turn, room mic     <- cost of the microphone alone
    whole clip, headset    <- cost of overlap alone
    whole clip, room mic   <- what the shipped demo actually measures

Run:
    python scripts/ablate_real_audio.py
    python scripts/ablate_real_audio.py --turns 60 --model small
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.pipeline.transcribe import FasterWhisperTranscriber  # noqa: E402
from evaluate_asr import term_recall, word_error_rate  # noqa: E402
from fetch_real_audio import IHM_SHARD, download_shard, load_rows  # noqa: E402

SDM_SHARD = "sdm/test-00000-of-00004.parquet"
SR = 16_000


def turn_key(row: dict) -> tuple[float, float]:
    """Identify a turn by its timing; both conditions annotate the same spans."""
    return round(row["begin"], 2), round(row["end"], 2)


def write_clip(samples: np.ndarray, path: Path) -> Path:
    import soundfile as sf

    peak = float(np.max(np.abs(samples))) or 1.0
    sf.write(str(path), samples * (0.70 / peak), SR, subtype="PCM_16")
    return path


def score_turns(transcriber, rows: list[dict], tmp: Path) -> dict:
    """Transcribe each turn as an isolated file and score it."""
    words = edits = 0
    recalls: list[float] = []
    for index, row in enumerate(rows):
        audio = write_clip(row["samples"], tmp / f"turn-{index}.wav")
        hypothesis = " ".join(s["text"] for s in transcriber.transcribe(audio).segments)
        wer = word_error_rate(row["text"], hypothesis)
        count = len(row["text"].split())
        words += count
        edits += round(wer * count)
        recalls.append(term_recall(row["text"], hypothesis))
    return {
        "words": words,
        "wer": edits / max(words, 1),
        "term_recall": statistics.fmean(recalls) if recalls else 0.0,
        "turns": len(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sdm-shard", default=SDM_SHARD)
    parser.add_argument("--ihm-shard", default=IHM_SHARD)
    parser.add_argument("--meeting", default="EN2002a",
                        help="meeting id present in both conditions")
    parser.add_argument("--turns", type=int, default=40,
                        help="how many matched turns to sample")
    parser.add_argument("--min-seconds", type=float, default=2.0)
    parser.add_argument("--max-seconds", type=float, default=6.0)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="en",
                        help="pin the language: autodetect picked Welsh on exactly "
                             "this audio and tripled the error rate")
    args = parser.parse_args()

    transcriber = FasterWhisperTranscriber(args.model, "auto", "int8",
                                           language=args.language)
    available, detail = transcriber.available()
    if not available:
        print(f"transcription unavailable: {detail}", file=sys.stderr)
        return 2

    print(f"model: {args.model}  language: {args.language}")
    print(f"loading {args.meeting} from both microphone conditions …")
    sdm_rows = load_rows(download_shard(args.sdm_shard), 1).get(args.meeting, [])
    ihm_rows = load_rows(download_shard(args.ihm_shard), 1).get(args.meeting, [])
    if not sdm_rows or not ihm_rows:
        print(f"{args.meeting} not present in both shards", file=sys.stderr)
        return 2

    sdm = {turn_key(r): r for r in sdm_rows}
    ihm = {turn_key(r): r for r in ihm_rows}

    candidates = [
        key for key in set(sdm) & set(ihm)
        if args.min_seconds <= key[1] - key[0] <= args.max_seconds
    ]
    if not candidates:
        print("no matched turns in the requested duration band", file=sys.stderr)
        return 2
    random.Random(0).shuffle(candidates)  # reproducible sample
    keys = sorted(candidates[: args.turns])
    print(f"{len(keys)} matched turns of {args.min_seconds:g}–{args.max_seconds:g}s, "
          f"same transcripts in both conditions\n")

    with tempfile.TemporaryDirectory(prefix="ablate-") as tmp_name:
        tmp = Path(tmp_name)
        ihm_result = score_turns(transcriber, [ihm[k] for k in keys], tmp)
        sdm_result = score_turns(transcriber, [sdm[k] for k in keys], tmp)

    # The clip-level numbers come from evaluate_asr.py on data/real-audio and
    # data/real-audio/headset-control; re-deriving them here would transcribe the
    # same hour of audio a second time.
    print(f"{'condition':<34}{'WER':>9}{'termRec':>10}{'words':>8}")
    print("-" * 61)
    print(f"{'per-turn, headset mic (isolated)':<34}{ihm_result['wer']:>8.1%}"
          f"{ihm_result['term_recall']:>10.1%}{ihm_result['words']:>8}")
    print(f"{'per-turn, distant mic (isolated)':<34}{sdm_result['wer']:>8.1%}"
          f"{sdm_result['term_recall']:>10.1%}{sdm_result['words']:>8}")

    print(f"\ncost of the microphone alone: "
          f"{sdm_result['wer'] - ihm_result['wer']:+.1%} WER "
          f"({ihm_result['wer']:.1%} → {sdm_result['wer']:.1%}) on identical text")
    print("cost of speaker overlap: measured by comparing these isolated numbers "
          "with the clip-level scores in data/real-audio/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
