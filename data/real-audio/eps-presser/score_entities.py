"""Adjudicate the proper nouns, because the caption reference cannot.

The WER computed against YouTube's auto-captions turns out to be dominated by
the *reference's* errors, not the app's: the captions render "Edmonton" as
"evington", "Ling Huang" as "lang hang", "Technology North" as "nors uh",
"autism" as "ai", and "body-worn" as "bodyw warn". Scoring against that
punishes the transcript for being right.

So this script does what an analyst would do: take the small set of entities we
can verify independently — from the email thread, the video title, and the
agency involved — and count who gets each one right.

Ground truth here is external knowledge, not a transcript:
  Technology North        the channel/company (video uploader, "Technology North")
  Edmonton Police Service the agency in the video title
  Ling Huang              the TN contact whose email started this work
  Cliff20                 TN's programme name (from the related videos)
  autism / spectrum       the subject
  body-worn video         what the programme redacts
  redaction               the service
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent.parent / "backend" / "scripts"),
)
from evaluate_asr import normalise  # noqa: E402

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-Actor": "demo.admin", "X-Role": "admin"}
RID = sys.argv[1]

# entity -> (correct forms, known mangled forms seen in the captions)
ENTITIES = {
    "Technology North": (["technology north"], ["nors uh", "north tech"]),
    "Edmonton Police Service": (["edmonton police"], ["evington police", "dington"]),
    "Ling Huang": (["ling huang"], ["lang hang", "lingan", "l lingan"]),
    "autism": (["autism"], ["ai "]),
    "body-worn video": (["body worn", "body worn video"], ["bodyw warn", "body warn"]),
    "redaction": (["redaction", "redact"], ["reduction"]),
    "spectrum": (["spectrum"], []),
    "employment": (["employment", "employ"], []),
}


def get(path: str) -> dict:
    return json.load(
        urllib.request.urlopen(urllib.request.Request(f"{BASE}{path}", headers=HEADERS))
    )


def count(text: str, forms: list[str]) -> int:
    return sum(text.count(f) for f in forms)


def main() -> None:
    ref = normalise(
        " ".join(
            s["text"]
            for s in json.loads((Path(__file__).resolve().parent / "eps_presser.transcript.json").read_text())[
                "segments"
            ]
        )
    )
    variants = {}
    for v in ("original", "denoised"):
        variants[v] = normalise(
            " ".join(s["text"] for s in get(f"/api/recordings/{RID}/transcript?variant={v}")["segments"])
        )

    print("Correct mentions per entity (higher is better; ground truth is the real world)\n")
    hdr = f"{'entity':26s} {'captions':>9s} {'original':>9s} {'denoised':>9s}"
    print(hdr)
    print("-" * len(hdr))

    totals = {"captions": 0, "original": 0, "denoised": 0}
    wrong = {"captions": 0, "original": 0, "denoised": 0}

    for name, (good, bad) in ENTITIES.items():
        c_ref = count(ref, good)
        c_orig = count(variants["original"], good)
        c_den = count(variants["denoised"], good)
        w_ref = count(ref, bad) if bad else 0
        w_orig = count(variants["original"], bad) if bad else 0
        w_den = count(variants["denoised"], bad) if bad else 0

        totals["captions"] += c_ref
        totals["original"] += c_orig
        totals["denoised"] += c_den
        wrong["captions"] += w_ref
        wrong["original"] += w_orig
        wrong["denoised"] += w_den

        print(f"{name:26s} {c_ref:9d} {c_orig:9d} {c_den:9d}")

    print("-" * len(hdr))
    print(f"{'TOTAL correct':26s} {totals['captions']:9d} {totals['original']:9d} {totals['denoised']:9d}")
    print(f"{'TOTAL mangled':26s} {wrong['captions']:9d} {wrong['original']:9d} {wrong['denoised']:9d}")

    Path("/tmp/eps/entity_scores.json").write_text(
        json.dumps({"correct": totals, "mangled": wrong}, indent=2)
    )


if __name__ == "__main__":
    main()
