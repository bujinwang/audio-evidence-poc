# Real recordings — AMI Meeting Corpus

Three clips of **real human speech recorded through a real room microphone**,
with **human-written transcripts**. Everything else in this repository is
synthetic; these exist to answer a question synthetic audio cannot: how well does
the pipeline actually transcribe people talking in a room?

| File | Duration | Turns | Speakers |
|---|---|---|---|
| `AMI_EN2002a_sdm.wav` | 94.0 s | 91 | 4 |
| `AMI_EN2002b_sdm.wav` | 92.5 s | 79 | 4 |
| `AMI_EN2002c_sdm.wav` | 90.0 s | 45 | 3 |

`headset-control/` holds the same three meetings captured through each
participant's headset microphone, for the controlled comparison below. Two of the
three match turn-for-turn (91/91 and 79/79); `EN2002c` matches only 26 of 45 turns
(137 of 269 words), because the two conditions segment some turns differently. The
matched subset averages 5.3 words per turn against 6.9 for the excluded turns, so
it is not a like-for-like sample — shorter utterances carry less context and
usually score *worse*, which makes that row the least trustworthy of the three
rather than a flattering one.

## Licence and attribution

**CC BY 4.0** — free to use and redistribute with attribution.

> Carletta, J. et al. (2006). *The AMI Meeting Corpus: A Pre-announcement.*
> In Machine Learning for Multimodal Interaction (MLMI 2005), LNCS 3869.

Source: `edinburghcstr/ami` on the Hugging Face Hub, config `sdm` (single distant
microphone) and `ihm` (individual headset microphones). The meetings are
English-language project-design meetings among four participants.

## These are reconstructed, not raw

Each clip is **assembled**, and this matters for how the numbers may be described:

> Corpus utterances are placed at their annotated offsets on a silent bed. The
> timeline and the pauses are real — the *gaps* are not. Whatever happened between
> utterances (cross-talk tails, room tone, chairs, doors) is absent.

So: honest material for testing transcription accuracy and timestamp behaviour.
**Not** honest material for claiming "this is what a real exhibit sounds like".
The `.transcript.json` sidecar says so per file, and `manifest.json` carries
`"reconstructed": true`.

Regenerate with:

```bash
cd backend
python scripts/fetch_real_audio.py --headset        # needs pyarrow + soundfile
```

## Measured accuracy — and why the headline number needed decomposing

`small` model, English pinned, strict scoring, CPU int8:

| Condition | WER | Content-term recall |
|---|---|---|
| Room mic, whole clips (shipped demo set) | **55.4%** | 61.5% |
| Headset mic, whole clips | 50.2% | 68.7% |
| Room mic, one utterance per file | 46.3% | 66.7% |
| Headset mic, one utterance per file | 22.9% | 86.2% |

A 55% WER next to the 3.9% the same system scores on synthetic audio demands an
explanation, not a caveat. The controlled comparison supplies it. AMI publishes
the same meetings through two microphone conditions with identical human
transcripts and identical segment boundaries, so holding the *text* fixed and
varying only the capture isolates the cause:

```
                                    WER
  headset mic, isolated speech     22.9%   <- what the ASR can do
  distant mic, isolated speech     46.3%   <- +23.4 pts: the microphone
  distant mic, whole clip          55.4%   <- +9.1 pts: speaker overlap
```

Three findings, in order of how much they matter:

1. **The microphone is the dominant term, not the language model.**
   `medium` — 3× the compute — moves the distant-mic number from 46.3% to 43.2%,
   a 3-point gain against a 23-point microphone penalty. Buying a bigger model is
   the wrong lever for this problem; buying a better microphone is the right one.
   For evidence work that argues for capture guidance in the intake workflow.

2. **Speaker overlap costs ~9 points on top**, and it is unavoidable in a single
   stream: one ASR pass cannot emit two speakers at once. In these windows 75–92%
   of turns overlap at least one other turn (90% of the transcript's words sit in
   such turns), so this is not a corner case. `DIARIZATION=true` does not fix it —
   diarization labels a single stream after the fact; it does not separate the
   sources before recognition.

3. **Content-term recall is much better than WER suggests** (61.5% vs 55.4% on the
   room-mic set, 86.2% on isolated headset speech). WER is sequence-aligned and
   charges full price for word order and function words; a keyword search does
   not. Names and nouns — the words an investigator searches for — survive more
   often than the headline number implies. This is why `evaluate_asr.py` now
   reports both.

### What that means at the search box

The metric that matters is whether a passage can be *found*, so these clips were
ingested and queried. Searching the machine transcript of these three meetings:

| Query | Top result | Verdict |
|---|---|---|
| `object file offline` | *"No, a store is an object file."* @ 27.4 s | ✅ found |
| `talk time methods one number` | *"…lots of methods that just return one number at a time"* @ 4.0 s | ✅ found |
| `computer room key front door` | *"Can you get in the front door?"* @ 26.0 s | ✅ found |
| `milestones target prototype` | *"Best fall on target."* @ 12.5 s | ❌ the audio says "milestones", the ASR heard "Best fall" |

That last row is the honest limit in one line: **search cannot find a word the
transcript never contained.** Recall is bounded by ASR recall, which is why
`term_recall` is reported next to WER and why the review-and-correct step is not
optional. No ranking improvement recovers this — only a better transcript does.

### Two measurement traps worth recording

**Language autodetection silently failed.** Whisper identified
`AMI_EN2002c_sdm.wav` as **Welsh** at p=0.73 and emitted `Erareth i cooks, maen
nhw'n ymlaen nefam…`. Forcing English took that clip from 77.0% to 40.4% WER and
the corpus from 65.5% to 55.8%. Nothing in the output said "wrong language" — it
looked like a bad transcript, not a bug. `WHISPER_LANGUAGE` now defaults to `en`
and autodetect is opt-in. A transcript in the wrong language is worse than no
transcript, because it looks like a result.

**Denoising was being handicapped by its own gain.** DeepFilterNet returns a
heavily attenuated signal — measured 27% of input RMS on `AMI_EN2002b_sdm.wav`,
with the active-speech fraction collapsing from 47% to 16%. Whisper reacts badly
to that: the same clip scored **95.2%** WER denoised versus **72.8%** once the
level was restored, with the filtering byte-identical. `pipeline/denoise.py` now
RMS-matches the derivative to the source. Every denoised number recorded before
this fix measured a level drop, not denoising.

### Denoising still does not help here

After the level fix, on the real distant-mic clips:

| File | WER | WER denoised | Δ |
|---|---|---|---|
| `AMI_EN2002a_sdm.wav` | 63.5% | 78.2% | **+14.7%** |
| `AMI_EN2002b_sdm.wav` | 56.5% | 72.8% | **+16.3%** |
| `AMI_EN2002c_sdm.wav` | 39.6% | 43.7% | **+4.1%** |

Denoising makes real room-mic audio substantially worse, on every file. The
mechanism is the same one visible on the synthetic set: suppression punches holes
in low-energy speech and Whisper fills them with plausible words. On the synthetic
noisy set the level fix turned the earlier regression into an exact tie — all six
files now score Δ ±0.0%. On genuinely difficult audio it is a clear loss.

**Treat denoising as a listening aid, not a transcription aid.** A reviewer can
hear a cleaned copy; the original remains the reference transcript.

## Honest limits of this evaluation

- **Three clips, one meeting series.** Not a benchmark. Enough to decompose the
  error and to catch regressions, not enough to claim an accuracy figure.
- **Reconstruction removes cross-talk context.** Utterances are placed on
  silence, so the ASR hears cleaner boundaries than a raw recording would have.
  That flatters the overlap penalty slightly.
- **AMI is not evidence audio.** Cooperative meeting speech, high-quality
  capture, no radio traffic, no cassette generation loss, no accents outside the
  corpus's own variety. Case audio will be harder.
- **The reference is disfluent ALL-CAPS speech** (`THREE IS ALRIGHT POSSIBLY IT
  WAS WHAT TIME'S D. N. L. G.`) while ASR emits punctuated fluent text. Strict
  scoring charges for that; `--tolerant` reports the other convention.
- **Speaker labels come from the corpus**, never from the app. Nothing here
  identifies anyone by voice.

Reproduce every number above:

```bash
cd backend
python scripts/evaluate_asr.py --dir ../data/real-audio --model small
python scripts/evaluate_asr.py --dir ../data/real-audio/headset-control --model small
python scripts/evaluate_asr.py --dir ../data/real-audio --model small --denoise
python scripts/ablate_real_audio.py --turns 40
```
