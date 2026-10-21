# Public audio library — provenance

The demo library is made of real recordings. Nothing here is synthesised: no TTS,
no AI voice, no generated transcript. Every clip was downloaded from a public
YouTube upload, cut to 2–3 minutes, downmixed to mono 44.1 kHz PCM, and
peak-normalised to about -1.5 dBFS so the four clips sit at a similar listening
level. The trims and gains are reproducible with `scripts/fetch_public_audio.sh`.

| Clip | Category | Source | Channel | Published |
|---|---|---|---|---|
| `2022-09-07_911_call_phoenix.wav` | Crime scene | [youtube.com/watch?v=A_JcfrFcabk](https://www.youtube.com/watch?v=A_JcfrFcabk) | Phoenix Police Department | 2022-09-07 |
| `2025-11-15_sport_football-championship.wav` | Sport, people talking | [youtube.com/watch?v=6QnA3dtbKZ0](https://www.youtube.com/watch?v=6QnA3dtbKZ0) | Buffalo Toronto Public Media | 2025-11-15 |
| `2026-05-26_speech_nasa-moonbase.wav` | Public speech | [youtube.com/watch?v=Nyki6O1oxIg](https://www.youtube.com/watch?v=Nyki6O1oxIg) | NASA | 2026-05-26 |
| `2025-07-08_speech_commencement-northwestern.wav` | Public speech | [youtube.com/watch?v=UgUntNCldAY](https://www.youtube.com/watch?v=UgUntNCldAY) | Northwestern University | 2025-07-08 |

## What each clip contains

**`2022-09-07_911_call_phoenix.wav` — 2:39, `phone_call`**
A call taker walks through a 911 call she took at 6 a.m.: a woman whispering from
inside her child's bedroom while her attacker moved through the house. The actual
call audio sits under the narration, quiet and clipped by the phone channel. This
is the hardest clip in the library and the closest to the product's real use case:
a single speaker, low level, telephone bandwidth, and speech that matters.

**`2025-11-15_sport_football-championship.wav` — 3:00, `sports`**
The opening of the 2025 Class AA high-school football championship at Highmark
Stadium, called from the booth by two commentators. Continuous overlapping
speech over stadium ambience: turn-taking, names, numbers, crowd noise under
every line. Useful for testing how retrieval handles two speakers who never pause
cleanly.

**`2026-05-26_speech_nasa-moonbase.wav` — 3:00, `speech`**
A NASA news conference on the Moon Base missions. Formal read-aloud speech with
technical vocabulary (landers, payloads, retroreflectors). Clean, wideband, one
speaker — the easy end of the range.

**`2025-07-08_speech_commencement-northwestern.wav` — 3:00, `speech`**
A commencement address, trimmed to a stretch of continuous delivery with laughter
and applause. Conversational register, punchlines, short sentences — a good
contrast to the NASA briefing.

The two speech clips are deliberately different in register. One is read-aloud
institutional speech, the other is a person performing for a room.

## Rights — read this before publishing the demo

The four clips are not on the same footing, and the difference matters because
this project has been shared through a public tunnel link.

| Clip | Status |
|---|---|
| NASA news conference | **Public domain.** A US federal government work; NASA's media guidelines allow reuse. Safe to publish. |
| Phoenix PD 911 call | **Released public record.** The department published the call itself. Attribution to the department is the right courtesy. Reuse is normally fine, but confirm the release terms if this goes commercial. |
| Football championship | **© Buffalo Toronto Public Media.** An excerpt of a local public-broadcast production. Fine for an internal demo; not licensed for redistribution. |
| Commencement address | **© Northwestern University.** Same position: fine internally, not licensed for redistribution. |

Practical consequence: if the demo is put behind a public link again, either keep
it to the NASA and 911 clips, or swap the two copyrighted excerpts for
Creative-Commons or public-domain material. An internal walkthrough with all four
is a normal fair-use-style demo use; a public URL is a different question.

## Rebuilding

```bash
scripts/fetch_public_audio.sh            # download and cut into data/public-audio/
scripts/fetch_public_audio.sh --upload   # ...and ingest into a running API
```

Downloads are cached by filename: delete a clip from `data/public-audio/` to force
it to be rebuilt. The library keeps its own copy of each original under
`data/originals/<recording-id>/`, so the staging folder can be deleted once the
clips are ingested.

Transcripts are not shipped. Each recording is transcribed on ingest by
faster-whisper, and denoised in parallel when DeepFilterNet is installed, which
gives the review screen a real original-vs-denoised comparison rather than a
synthetic one.
