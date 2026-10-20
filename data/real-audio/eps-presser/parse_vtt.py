"""Parse YouTube rolling auto-captions (VTT with inline <c> word timings).

The caption track repeats each line: one cue carries the previous line as
carry-over text, the next carries the same text plus the *new* words annotated
with inline timestamps like <00:00:04.279><c> you</c>. So the payload that has
tags is the only one with new information; everything else is a repeat.

Output: a flat list of (start, end, word) for the whole video.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
TAG = re.compile(r"<(\d{2}):(\d{2}):(\d{2})\.(\d{3})><c>([^<]*)</c>")


def ts_to_s(m):
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def cue_span(line: str) -> tuple[float, float] | None:
    found = TS.findall(line)
    if len(found) < 2:
        return None
    a = ts_to_s(TS.search(line))
    b = list(TS.finditer(line))[-1]
    return a, ts_to_s(TS.match(b.group(0), 0)) if False else (
        a,
        int(b.group(1)) * 3600
        + int(b.group(2)) * 60
        + int(b.group(3))
        + int(b.group(4)) / 1000.0,
    )


def main(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]

    words: list[dict] = []
    seen: set[str] = set()
    last_end = 0.0

    for block in blocks:
        lines = [ln for ln in block.splitlines()]
        if not lines or "-->" not in lines[0]:
            continue
        span = cue_span(lines[0])
        if span is None:
            continue
        cue_start, cue_end = span
        payload_lines = lines[1:]

        # The first payload line is carry-over text restating the previous cue.
        # Only lines that actually carry inline timestamps hold new words.
        for pline in payload_lines:
            if "<" not in pline:
                continue

            # Words before the first tag on THIS line have no timestamp of
            # their own; they begin at the cue start.
            first_tag = pline.find("<")
            lead = TAG.sub("", pline[:first_tag]).strip()
            for w in lead.split():
                key = f"{cue_start:.3f}|{w}"
                if key not in seen:
                    seen.add(key)
                    words.append({"start": cue_start, "word": w})

            for m in TAG.finditer(pline):
                w = m.group(5).strip()
                if not w:
                    continue
                start = (
                    int(m.group(1)) * 3600
                    + int(m.group(2)) * 60
                    + int(m.group(3))
                    + int(m.group(4)) / 1000.0
                )
                # Rolling captions restate the same word in several cues; the
                # word start time is the dedupe key (restatements share it).
                key = f"{start:.3f}|{w}"
                if key in seen:
                    continue
                seen.add(key)
                words.append({"start": start, "word": w})
                last_end = max(last_end, start)

    words.sort(key=lambda x: x["start"])

    # End of a word = start of the next one; the last word gets the cue end.
    for i, w in enumerate(words):
        nxt = words[i + 1]["start"] if i + 1 < len(words) else last_end + 0.3
        w["end"] = max(nxt, w["start"] + 0.05)

    print(f"words: {len(words)}", file=sys.stderr)
    if words:
        print(f"span: {words[0]['start']:.2f}s .. {words[-1]['end']:.2f}s", file=sys.stderr)
    (HERE / "words.json").write_text(
        json.dumps(words, indent=1), encoding="utf-8"
    )

    joined = " ".join(w["word"] for w in words)
    (HERE / "transcript_flat.txt").write_text(joined, encoding="utf-8")
    print(joined[:600], file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
