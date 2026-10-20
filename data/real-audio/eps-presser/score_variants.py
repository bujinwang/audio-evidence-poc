"""Score the app's transcripts for the EPS presser against the caption reference.

Reuses the repo's own scoring functions (`evaluate_asr.py`) rather than writing
new ones — they are already tested and their conventions are documented.

Caveat that must travel with every number here: the reference is YouTube's
*automatic* captions, i.e. another ASR system, not a human transcript. This
therefore measures **disagreement between two ASR systems**, which is a lower
bound on true error, not a WER. Term recall is the more trustworthy signal.
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

from evaluate_asr import (  # noqa: E402
    char_error_rate,
    content_terms,
    normalise,
    term_recall,
    word_error_rate,
)

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-Actor": "demo.admin", "X-Role": "admin"}
RID = sys.argv[1]


def get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", headers=HEADERS)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main() -> None:
    rec = get(f"/api/recordings/{RID}")
    prov = get(f"/api/recordings/{RID}/provenance")

    ref_payload = json.loads((Path(__file__).resolve().parent / "eps_presser.transcript.json").read_text())
    ref_text = " ".join(s["text"] for s in ref_payload["segments"])

    hyp: dict[str, str] = {}
    for variant in ("original", "denoised"):
        try:
            t = get(f"/api/recordings/{RID}/transcript?variant={variant}")
            hyp[variant] = " ".join(s["text"] for s in t["segments"])
        except Exception as exc:  # noqa: BLE001
            print(f"{variant}: unavailable ({exc})")

    print(f"file:            {rec['filename']}")
    print(f"duration:        {rec['duration_s']:.1f}s ({rec['duration_s']/60:.1f} min)")
    print(f"segments:        {rec['segment_count']}")
    print(f"reference words: {len(normalise(ref_text).split())} (YouTube auto-captions)")
    print(f"intake mode:     {prov['intake_mode']}")
    print()
    print(f"{'variant':10s} {'words':>6s} {'WER':>8s} {'CER':>8s} {'termRec':>8s}")
    print("-" * 44)

    scores: dict[str, dict] = {}
    for variant, text in hyp.items():
        row = {
            "words": len(normalise(text).split()),
            "wer": word_error_rate(ref_text, text),
            "cer": char_error_rate(ref_text, text),
            "term_recall": term_recall(ref_text, text),
        }
        scores[variant] = row
        print(
            f"{variant:10s} {row['words']:6d} {row['wer']:7.1%} "
            f"{row['cer']:7.1%} {row['term_recall']:7.1%}"
        )

    if "original" in scores and "denoised" in scores:
        d = scores["denoised"]["wer"] - scores["original"]["wer"]
        verdict = "worse" if d > 0.005 else "better" if d < -0.005 else "tie"
        print(f"\ndenoising effect on WER: {d:+.1%} ({verdict})")

    # --- the terms that matter for retrieval ----------------------------- #
    print("\nDid the terms that matter survive?\n")
    concepts = [
        ("Technology North", ["technology north"]),
        ("Edmonton Police Service", ["edmonton police"]),
        ("autism / spectrum", ["autism", "spectrum", "autistic"]),
        ("Cliff20", ["cliff 20", "cliff20"]),
        ("body-worn video", ["body worn"]),
        ("neurodiverse", ["neurodiverse", "neuro diverse"]),
        ("redaction", ["redaction", "redact"]),
    ]
    ref_flat = normalise(ref_text)
    print(f"{'concept':26s} {'in ref':8s} {'original':10s} {'denoised':10s}")
    print("-" * 58)
    failures = []
    for label, forms in concepts:
        in_ref = any(f in ref_flat for f in forms)
        cells = []
        for variant in ("original", "denoised"):
            flat = normalise(hyp.get(variant, ""))
            ok = any(f in flat for f in forms)
            cells.append("yes" if ok else "MISSING")
            if in_ref and not ok:
                failures.append((variant, label))
        print(f"{label:26s} {'yes' if in_ref else 'no':8s} {cells[0]:10s} {cells[1]:10s}")

    if failures:
        print("\nLost terms (present in reference, absent in transcript):")
        for variant, label in failures:
            print(f"  - {variant}: {label}")

    Path("/tmp/eps/asr_scores.json").write_text(json.dumps(scores, indent=2))


if __name__ == "__main__":
    main()
