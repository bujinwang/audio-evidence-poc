#!/usr/bin/env python3
"""Fetch real, openly licensed meeting audio with human transcripts.

Why this exists: every recording in the demo and noisy sets is synthetic. That
proves the plumbing works, but it says nothing about accuracy on real voices.
The AMI Meeting Corpus supplies both halves of what an honest evaluation needs —
real human speech through a real room microphone, and a human-written transcript
with speaker labels and timings.

Source: **AMI Meeting Corpus**, CC BY 4.0, via the `edinburghcstr/ami` mirror on
the Hugging Face Hub. Config `sdm` = single distant microphone: the mix as heard
in the room, far closer to a real recording than a headset mic.

How the clips are built — this matters for how they may be described:

    Each row of the corpus is one annotated utterance (a few seconds) with its
    true begin/end offset. We place those utterances at their annotated offsets
    on a silent bed, so the result keeps the meeting's real timeline and real
    pauses. It is **not** a continuous original recording: whatever happened
    between utterances (cross-talk, room tone, chairs) is absent.

    Honest material for testing transcription and timestamps. Dishonest material
    for claiming "this is what a real recording sounds like". The manifest says
    so per file — do not present these as genuine exhibits.

Why a whole shard and not streaming: `datasets` streaming spent five minutes
without fetching a single row (large row groups, one round trip per batch). The
shard is 245 MB and downloads in seconds, so we take the predictable path.

Requires (dev-only, not runtime dependencies):
    pip install pyarrow soundfile huggingface_hub

Usage:
    python scripts/fetch_real_audio.py --out ../data/real-audio
    python scripts/fetch_real_audio.py --meetings 4 --seconds 120
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

DATASET = "edinburghcstr/ami"
SHARD = "sdm/test-00000-of-00004.parquet"
IHM_SHARD = "ihm/test-00000-of-00004.parquet"
SR = 16_000

CITATION = (
    "Carletta, J. et al. (2006). The AMI Meeting Corpus: A Pre-announcement. "
    "In Machine Learning for Multimodal Interaction (MLMI 2005), LNCS 3869."
)


def download_shard(shard: str) -> Path:
    """Return a local shard path — reuse an existing file, else fetch into the HF cache."""
    local = Path(shard).expanduser()
    if local.exists():
        print(f"using local shard {local}")
        return local

    from huggingface_hub import hf_hub_download

    print(f"fetching {DATASET}/{shard} …")
    return Path(hf_hub_download(repo_id=DATASET, filename=shard, repo_type="dataset"))


def decode_wav(payload: bytes) -> np.ndarray:
    """Decode an embedded WAV blob to mono float32 at 16 kHz.

    The corpus stores 32-bit **float** WAV (format 3), which the stdlib `wave`
    module refuses ("unknown format: 3"), so libsndfile does the decoding.
    """
    import io

    import soundfile as sf

    data, rate = sf.read(io.BytesIO(payload), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if rate != SR:
        target = int(len(data) / rate * SR)
        data = np.interp(
            np.linspace(0, len(data) - 1, target), np.arange(len(data)), data
        ).astype(np.float32)
    return data


def load_rows(shard_path: Path, limit_meetings: int) -> dict[str, list[dict]]:
    """Read the shard and group utterances by meeting."""
    import pyarrow.parquet as pq

    table = pq.read_table(
        shard_path,
        columns=["meeting_id", "speaker_id", "text", "begin_time", "end_time", "audio"],
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    wanted: set[str] = set()

    for row in table.to_pylist():
        meeting_id = row["meeting_id"]
        if meeting_id not in wanted:
            if len(wanted) >= limit_meetings:
                continue
            wanted.add(meeting_id)
        grouped[meeting_id].append(
            {
                "begin": float(row["begin_time"]),
                "end": float(row["end_time"]),
                "text": (row["text"] or "").strip(),
                "speaker": row["speaker_id"],
                "samples": decode_wav(row["audio"]["bytes"]),
            }
        )
    return dict(grouped)


def select_window(rows: list[dict], target_seconds: float) -> list[dict]:
    """The densest `target_seconds` window of utterances, in time order."""
    rows = sorted(rows, key=lambda r: r["begin"])
    if not rows:
        return []
    best_start, best_count = rows[0]["begin"], -1
    for candidate in rows:
        window_end = candidate["begin"] + target_seconds
        count = sum(1 for r in rows if candidate["begin"] <= r["begin"] < window_end)
        if count > best_count:
            best_start, best_count = candidate["begin"], count
    window_end = best_start + target_seconds
    selected = [r for r in rows if best_start <= r["begin"] < window_end]
    if len(selected) < 8:  # sparse meeting — fall back to the first N
        selected = rows[: max(8, min(len(rows), 30))]
    return selected


def _pick_rows(rows: list[dict], keys: list[tuple] | None) -> list[dict]:
    """Restrict to the utterances identified by `keys` = [(begin, end), ...].

    The IHM control must contain the *same* turns as the SDM clip so the only
    variable is the microphone. The two conditions annotate the same meeting with
    the same segment boundaries, so matching on the timing pair identifies the
    same turns. A small tolerance absorbs rounding in the published timings.
    """
    if keys is None:
        return rows
    chosen = []
    for row in rows:
        for begin, end in keys:
            if abs(row["begin"] - begin) < 0.02 and abs(row["end"] - end) < 0.02:
                chosen.append(row)
                break
    return chosen or rows


def build_clip(meeting_id: str, rows: list[dict], out_dir: Path,
               target_seconds: float, *, drop_overlap: bool = False,
               suffix: str = "", condition: str = "sdm",
               keys: list[tuple] | None = None) -> dict:
    rows = sorted(rows, key=lambda r: r["begin"])
    if not rows:
        raise ValueError(f"{meeting_id}: no utterances")

    # Utterances are scattered across an hour-long meeting, so "the first N"
    # captures a few words separated by minutes of silence. Take the densest
    # window instead: the start time that puts the most utterances inside
    # `target_seconds`. That yields continuous-sounding conversation.
    if keys is not None:
        selected = sorted(_pick_rows(rows, keys), key=lambda r: r["begin"])
    else:
        selected = select_window(rows, target_seconds)

    if drop_overlap:
        # Control condition. Conversations overlap ~90% of the time in these
        # windows, and a single-stream ASR physically cannot emit two speakers
        # at once, so overlapping utterances impose a WER floor that has nothing
        # to do with acoustic quality. This keeps only the utterances that do
        # *not* overlap anything else.
        #
        # Caveat: stripping overlaps also leaves long silences, and Whisper
        # hallucinates or drops content in silence, so this control changes two
        # variables at once and cannot be read as "the cost of cross-talk".
        # Superseded by the IHM comparison below; kept because it still bounds
        # how little audio in these windows is overlap-free.
        selected = [
            r for r in selected
            if not any(
                o is not r and r["begin"] < o["end"] and o["begin"] < r["end"]
                for o in selected
            )
        ]
        if len(selected) < 8:
            return {}

    t0 = selected[0]["begin"]

    span = selected[-1]["end"] - t0
    buffer = np.zeros(int((span + 0.5) * SR), dtype=np.float32)
    segments = []

    for row in selected:
        start = int(round((row["begin"] - t0) * SR))
        clip = row["samples"]
        end = start + len(clip)
        if end > len(buffer):
            buffer = np.pad(buffer, (0, end - len(buffer)))
        buffer[start:end] += clip
        segments.append(
            {
                "start": round(row["begin"] - t0, 3),
                "end": round(row["end"] - t0, 3),
                # Absolute meeting-relative times, kept so a matched clip from
                # another microphone condition can select the same turns.
                "abs_begin": round(row["begin"], 3),
                "abs_end": round(row["end"], 3),
                "text": row["text"],
                "speaker": row["speaker"],
                "words": [],  # the corpus has no word-level alignment
            }
        )

    # Distant-mic excerpts are quiet (peak around 0.1), so always bring the clip
    # to a usable level rather than only taming overloads.
    peak = float(np.max(np.abs(buffer))) or 1.0
    buffer = buffer * (0.70 / peak)

    filename = f"AMI_{meeting_id}_{condition}{suffix}.wav"
    target = out_dir / filename
    import soundfile as sf

    sf.write(str(target), buffer, SR, subtype="PCM_16")

    sidecar = target.with_suffix(".transcript.json")
    sidecar.write_text(
        json.dumps(
            {
                "language": "en",
                "source": f"ami-corpus-{condition}",
                "note": (
                    "Human transcript from the AMI Meeting Corpus annotations. "
                    "Audio is reconstructed: corpus utterances placed at their "
                    "annotated offsets, with silence between them."
                    + (" Overlapping turns were removed, so this is a "
                       "single-speaker control clip, not a realistic mix."
                       if drop_overlap else "")
                ),
                "segments": segments,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "meeting_id": meeting_id,
        "audio": str(target),
        "sidecar": str(sidecar),
        "filename": filename,
        "source_type": "other",
        "recorded_at": None,
        "duration_s": round(len(buffer) / SR, 2),
        "segments": len(segments),
        "speakers": sorted({row["speaker"] for row in selected}),
        "reconstructed": True,
        "condition": condition,
        "cross_talk_removed": drop_overlap,
        "license": "CC BY 4.0",
        "citation": CITATION,
        "_turn_keys": [(s["abs_begin"], s["abs_end"]) for s in segments],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent.parent
                                             / "data" / "real-audio"))
    parser.add_argument("--shard", default=SHARD)
    parser.add_argument("--meetings", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--control", action="store_true",
                        help="also write a single-speaker control set with overlapping "
                             "turns removed — isolates how much WER is cross-talk")
    parser.add_argument("--headset", action="store_true",
                        help="also write the same clips from the IHM (headset mic) "
                             "condition: identical text, identical timings, cleaner audio. "
                             "The difference in WER is microphone quality alone")
    parser.add_argument("--ihm-shard", default=IHM_SHARD,
                        help="IHM shard for --headset (repo path or a local file)")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    shard_path = download_shard(args.shard)
    print(f"reading {shard_path.name} …")
    meetings = load_rows(shard_path, args.meetings)
    if not meetings:
        print("no meetings found in shard", file=sys.stderr)
        return 2

    manifest = []
    for meeting_id, rows in meetings.items():
        entry = build_clip(meeting_id, rows, out_dir, args.seconds)
        manifest.append(entry)
        print(f"  {entry['filename']:<24} {entry['duration_s']:>6.1f}s "
              f"{entry['segments']:>3} segs  speakers={','.join(entry['speakers'])}")

    (out_dir / "manifest.json").write_text(
        json.dumps([{k: v for k, v in e.items() if not k.startswith("_")} for e in manifest],
                   indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {len(manifest)} clips to {out_dir}")

    if args.headset:
        print("\nfetching the IHM (headset) condition for a matched comparison …")
        ihm_shard = download_shard(args.ihm_shard)
        ihm_meetings = load_rows(ihm_shard, args.meetings)

        ihm_dir = out_dir / "headset-control"
        ihm_dir.mkdir(parents=True, exist_ok=True)
        ihm_manifest = []
        for entry in manifest:
            meeting_id = entry["meeting_id"]
            rows = ihm_meetings.get(meeting_id, [])
            if not rows:
                print(f"  {meeting_id}: not in the IHM shard, skipped")
                continue
            matched = build_clip(meeting_id, rows, ihm_dir, args.seconds,
                                 condition="ihm", keys=entry["_turn_keys"])
            ihm_manifest.append(matched)
            print(f"  {matched['filename']:<28} {matched['duration_s']:>6.1f}s "
                  f"{matched['segments']:>3} of {entry['segments']} turns matched")
        (ihm_dir / "manifest.json").write_text(
            json.dumps([{k: v for k, v in e.items() if not k.startswith("_")}
                        for e in ihm_manifest], indent=2),
            encoding="utf-8",
        )
        print(f"wrote {len(ihm_manifest)} headset-control clips to {ihm_dir}")

    if args.control:
        control_dir = out_dir / "control"
        control_dir.mkdir(parents=True, exist_ok=True)
        control_manifest = []
        print()
        for meeting_id, rows in meetings.items():
            entry = build_clip(meeting_id, rows, control_dir, args.seconds,
                               drop_overlap=True, suffix="_control")
            if not entry:
                print(f"  {meeting_id}: not enough non-overlapping turns, skipped")
                continue
            control_manifest.append(entry)
            print(f"  {entry['filename']:<24} {entry['duration_s']:>6.1f}s "
                  f"{entry['segments']:>3} segs")
        (control_dir / "manifest.json").write_text(
            json.dumps(control_manifest, indent=2), encoding="utf-8"
        )
        print(f"wrote {len(control_manifest)} control clips to {control_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
