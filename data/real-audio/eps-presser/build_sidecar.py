"""Build a sidecar reference transcript from the parsed caption words.

Grouping strategy: rebuild the caption *lines* using the cue boundaries in the
VTT, because those are where YouTube's ASR chose to break — that is the most
honest available approximation of utterance boundaries. Each group becomes one
segment carrying its word timings, so the app can score ASR against it and seek
within it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
TAG = re.compile(r"<(\d{2}):(\d{2}):(\d{2})\.(\d{3})><c>([^<]*)</c>")


def sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def main() -> None:
    words = json.load(open(HERE / "words.json"))
    vtt = (HERE / "caps.en.vtt").read_text(encoding="utf-8").split("\n\n")

    # Rebuild lines: a line is complete when a cue's payload has text but no
    # tags (that is the trailing restatement cue YouTube emits).
    lines: list[dict] = []
    for block in vtt:
        rows = block.strip().splitlines()
        if not rows or "-->" not in rows[0]:
            continue
        spans = TS.findall(rows[0])
        if len(spans) < 2:
            continue
        start = sec(*spans[0])
        end = sec(*spans[-1])
        payload = rows[1:]
        if not payload:
            continue

        # The line that has tags is the one introducing new words.
        tagged = next((ln for ln in payload if "<" in ln), None)
        if tagged is None:
            continue

        # Word count of the *new* material = tags on this line + any lead words.
        n_tags = len(TAG.findall(tagged))
        lead = TAG.sub("", tagged[: tagged.find("<")]).strip()
        n_lead = len(lead.split()) if lead else 0
        n_new = n_tags + n_lead
        if n_new == 0:
            continue
        lines.append({"start": start, "end": end, "n": n_new})

    # Walk the words in order, handing n_new words to each line.
    idx = 0
    segments = []
    total = sum(x["n"] for x in lines)
    for ln in lines:
        chunk = words[idx : idx + ln["n"]]
        idx += ln["n"]
        if not chunk:
            continue
        text = " ".join(w["word"] for w in chunk).strip()
        if not text:
            continue
        segments.append(
            {
                "start": round(chunk[0]["start"], 3),
                "end": round(chunk[-1]["end"], 3),
                "text": text,
                "speaker": "SPEAKER_00",
                "words": [
                    {
                        "word": w["word"],
                        "start": round(w["start"], 3),
                        "end": round(w["end"], 3),
                    }
                    for w in chunk
                ],
            }
        )

    out = {
        "language": "en",
        "source": "youtube-auto-captions",
        "note": (
            "Reference transcript derived from YouTube's automatic captions for "
            "the Technology North / Edmonton Police Service press update "
            "(https://youtu.be/qSShW86up-A). Captions are machine-generated, so "
            "this is a reference for measuring ASR behaviour on real speech, NOT "
            "a human-verified transcript and not evidence of what was said. "
            "Word timings come from the caption track's inline <c> annotations."
        ),
        "segments": segments,
    }

    (HERE / "eps_presser.transcript.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    n_words = sum(len(s["words"]) for s in segments)
    print(f"lines: {len(lines)}  words assigned: {idx}/{len(words)} (total {total})")
    print(f"segments: {len(segments)}  words in segments: {n_words}")
    print(f"span: {segments[0]['start']} .. {segments[-1]['end']}")
    print("\nfirst 3 segments:")
    for s in segments[:3]:
        print(f"  [{s['start']:7.2f}-{s['end']:7.2f}] {s['text'][:90]}")


if __name__ == "__main__":
    main()
