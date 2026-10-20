# EPS / Technology North press update — the first real, on-topic example

**Source:** <https://youtu.be/qSShW86up-A> — *"Edmonton Police Service EPS provides
update on autism employment program December 18,2024"*, uploaded by Technology
North (2025-01-04). 30:17, public YouTube video.

**Why this recording matters more than the AMI clips.** The AMI corpus is
cooperative meeting speech between strangers. This is a real press conference
about the actual subject of the product: an autism employment programme, body-worn
video redaction, and the EPS/TN service agreement. It contains precisely the thing
that breaks ASR in practice — a dense stream of proper nouns nobody's language
model has seen, spoken by people who are nervous on camera.

## What was done

```bash
# 1. audio
yt-dlp -f bestaudio -x --audio-format wav -o eps_presser.%(ext)s https://youtu.be/qSShW86up-A
ffmpeg -i eps_presser.wav -ac 1 -ar 16000 -c:a flac eps_presser.flac   # 349 MB -> 33 MB

# 2. reference text from the caption track (word-level <c> timings)
yt-dlp --skip-download --write-auto-subs --sub-langs "en.*,en" -o caps.%(ext)s https://youtu.be/qSShW86up-A
python parse_vtt.py caps.en.vtt          # -> words.json (4,979 words, 4.20s..1813.9s)
python build_sidecar.py                  # -> eps_presser.transcript.json (688 segments)

# 3. score whatever the app produced (needs the recording ingested first)
python score_variants.py rec_<id>        # WER/CER/term-recall, original vs denoised
python score_entities.py rec_<id>        # proper-noun counts vs the captions
```

All four scripts resolve paths relative to this directory, so they run in place.
`caps.en.vtt`, `words.json` and the `.flac` are gitignored (large / regenerable);
the transcript JSON, scripts and this README are committed so the measurement can
be reproduced.

`parse_vtt.py` handles two things the naive parse gets wrong:

- **Rolling captions repeat.** Each cue restates the previous line as untagged
  text and carries the *new* words as `<c>` spans. Only tagged lines hold new
  words; dedupe on the word start time.
- **Lead words have no tag.** The first cue is literally
  `as<00:00:04.279><c> you</c>…` — the word "as" precedes any timestamp. Dropping
  untagged leading words lost 18 words before it was fixed.

## Results

Ingested through the normal path (full intake, `faster-whisper small`, English
pinned): **773 segments**, 30:17, denoised derivative produced, 7m 43s wall clock
for 30 minutes of stereo conference audio (~RTF 0.25 on CPU int8).

### The headline number is misleading, and that is the finding

Scored against the caption reference:

| Variant | Words | WER | CER | Term recall |
|---|---|---|---|---|
| original | 4,304 | 20.0% | 14.2% | 86.6% |
| denoised | 4,481 | 18.1% | 12.1% | 86.4% |

**Do not quote 20% as the ASR error rate.** The reference is YouTube's
*automatic* captions — another ASR system. This measures disagreement between two
machines, not truth. And the disagreement is dominated by the reference being
wrong. From the diff:

| Reference (captions) | Hypothesis (this app) | Reality |
|---|---|---|
| `evington` | `edmonton` | **Edmonton** |
| `lang hang`, `l lingan` | `ling huang` | **Ling Huang** |
| `nors uh` | `north` | **Technology North** |
| `ai` | `autism` | **autism** |
| `bodyw warn`, `warn` | `body worn` | **body-worn** |
| `reduction` | `redaction` | **redaction** |

Counting correct mentions of verifiable entities — ground truth from the video
title, the channel, and the related videos, not from either transcript:

| Entity | Captions | Original | Denoised |
|---|---|---|---|
| Technology North | 15 | 17 | 17 |
| Edmonton Police Service | 1 | 5 | 5 |
| Ling Huang | 0 | 2 | 2 |
| autism | 6 | 9 | 9 |
| body-worn video | 0 | 11 | 11 |
| redaction | 14 | 18 | 18 |
| spectrum | 14 | 13 | 14 |
| employment | 38 | 35 | 38 |
| **total correct** | **88** | **110** | **114** |
| **total mangled** | **20** | **6** | **6** |

The app's transcript is materially better than YouTube's on exactly the words an
investigator would search for, and mangles 6 instead of 20. WER scored against a
weaker reference penalises it for being right.

### Denoising: no reliable verdict on this recording

Whole-file WER says denoising helped by 1.9%. Split into ten ~3-minute windows,
that collapses into noise:

| Window | Original | Denoised | Winner |
|---|---|---|---|
| 0–181s | 5.6% | 6.6% | tie |
| 181–363s | 15.9% | 16.5% | tie |
| 363–545s | 21.6% | 21.0% | tie |
| 545–726s | 21.2% | 23.1% | original |
| 726–908s | 24.8% | **15.4%** | denoised |
| 908–1090s | 17.8% | 18.3% | tie |
| 1090–1272s | 20.4% | 18.8% | denoised |
| 1272–1453s | **30.1%** | **11.6%** | denoised |
| 1453–1635s | 27.3% | 38.6% | original |
| 1635–1817s | 23.2% | 23.0% | tie |

3 windows denoised, 2 original, 5 ties — and the swings (11.6% vs 38.6%) are
far larger than the net difference. Both variants make the **same** substitutions
in the same places; the difference is in insertions and deletions, i.e. how much
silence and crosstalk each pass decided to transcribe. That is segmentation
variance, not a filtering benefit. **Report the tie; do not claim denoising helps
here.** It is consistent with the AMI finding that denoising is a listening aid.

## Retrieval on real conference audio

This is the part that works, and it is the point of the product:

| Query | Top hit | Time |
|---|---|---|
| `Edmonton Police Service` | "which the Edmonton police service controls" | 938.2s |
| `body worn video redaction` | "in order to do body-worn video redaction?" | 26.7s |
| `Ling Huang` | "My name is Ling Ling Huang." | 225.3s |
| `autism employment program` | "autism employment across our border" | 310.8s |

Timestamps verified independently: transcoding 204–211s and re-running ASR on
that clip alone yields *"I'd like to introduce Ling Huang, who's the President and
CEO of Technology North"* — the audio really says that there. Range requests on
the 30-minute original return `206` with `content-range: bytes 0-2047/34983688`.

## Caveats to carry

- The reference transcript is **machine captions**, not human-verified. It is
  adequate for measuring entity recall and for locating passages; it is **not**
  evidence of what was said.
- The video is a public YouTube upload, used here as a development sample. Nothing
  in the app sends audio anywhere; all processing is local.
- Speaker labels are not assigned — the transcript is a single unnamed speaker
  stream even though at least four people speak. The transcript names Ling Huang
  ("founder and CEO of Technology North"), a "superintendent of the division",
  "Matthew Lambritz", and "Rob". The Chief is referred to only as "Chief" — the
  surname is **never spoken in the audio**, so it cannot be recovered from this
  recording. Do not infer it.
- `Cliff20` does not appear in the audio at all, so it cannot be used as a term
  test on this recording. Do not add it to a keyword list and expect a hit.
