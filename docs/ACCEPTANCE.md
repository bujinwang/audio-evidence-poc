# Acceptance criteria — evidence of completion

PRD §11, checked against the running build. Every row is covered by an automated
test unless marked *manual*.

Run the suite:

```bash
cd backend && ./.venv/bin/python -m pytest -q
# 113 passed
```

| # | Criterion | Status | How it is verified |
|---|---|---|---|
| 1 | An audio sample can be uploaded successfully | ✅ | `test_upload.py::test_upload_creates_searchable_recording` — 201, status `ready`, 5 segments, job `succeeded` |
| 2 | The original remains unchanged | ✅ | `test_upload.py::test_original_is_preserved_read_only_and_unmodified` — stored bytes == uploaded bytes, SHA-256 matches the API value, mode `0444`. `test_working_copy_is_a_separate_derivative` proves derivatives live elsewhere |
| 3 | Denoising and transcription jobs execute or clearly report unavailable dependencies | ✅ | **Executes**: with `faster-whisper` installed, an upload without a sidecar produced 7 segments with word timings in 4.3 s for 29 s of audio (`engine=faster-whisper`, `model=small`), searchable and seekable. **Reports**: `test_pipeline.py::test_denoiser_unavailable_is_reported_not_fatal` and `::test_transcription_unavailable_is_reported_not_fatal` — job ends `succeeded` / `completed_with_warnings`, reason stored in `processing_meta`, surfaced in `warnings` and in the UI |
| 4 | Original and denoised transcripts can be compared | ✅ | Verified with real ASR on both sides: a 4 dB warehouse recording produced `original` and `denoised` variants, each transcribed independently by `faster-whisper`, and `/comparison` returned `available: true` with per-segment similarity. Denoising is delay-compensated (`-D`) **and level-matched to the source** — measured 0-sample lag, and output RMS brought to within 1 dB of input so the comparison reflects filtering rather than gain. With no denoiser the endpoint returns `available: false` plus the reason; with only a reference transcript the copy is flagged `denoised_is_reference_copy` |
| 5 | Transcript segments have usable timestamps | ✅ | `test_pipeline.py::test_segments_have_usable_timestamps` — monotonic, positive duration, first segment at 0, within recording duration, word-level timings present |
| 6 | Keyword and semantic search return candidate passages | ✅ | `test_search.py::test_keyword_search_ranks_by_term_coverage`, `::test_semantic_search_returns_ranked_passages`, `::test_hybrid_search_fuses_both_signals` |
| 7 | A search result opens the original audio at the relevant timestamp | ✅ | `test_workflow_e2e.py::test_acceptance_workflow` — `Range: bytes=0-2047` returns `206` + `Content-Range`; hit `start_s` equals the transcript segment `start_s`; the UI sets `currentTime` on selection |
| 8 | A reviewer can correct and save a transcript segment | ✅ | `test_review_audit.py::test_edit_preserves_ai_text_and_records_a_revision` |
| 9 | The original AI text and revision history remain available | ✅ | Same test: `ai_text` unchanged, revision stores `prev_text`/`new_text`/actor/note. `::test_history_combines_revisions_and_audit_events` covers multiple passes |
| 10 | An authorized reviewer can explicitly mark a segment verified | ✅ | `test_review_audit.py::test_verification_is_explicit_and_attributed`; `::test_correction_does_not_imply_verification` proves the separation |
| 11 | Review actions are recorded in an audit trail | ✅ | `test_review_audit.py::test_audit_trail_captures_the_review_lifecycle`, `::test_audit_csv_export`, `::test_editing_a_verified_segment_invalidates_the_verification` |
| 12 | Automated tests cover the primary workflow | ✅ | `test_workflow_e2e.py` (6 tests) walks upload → search → timestamp playback → correction → verification → audit |

## Search: word-form matching

| Property | Evidence |
|---|---|
| The reported case: `small` finds "smaller" | `test_word_forms.py::test_short_form_finds_the_longer_word`, `::test_the_reported_pair_returns_the_same_passage` |
| Singular and plural are interchangeable in both directions | `::test_plural_and_singular_are_interchangeable` (truck/trucks, box/boxes, flag/flags) |
| Verb forms are interchangeable | `::test_every_verb_form_reaches_the_passage` (walk/walking/walked) |
| Precision is not traded away for recall | `::test_prefix_matching_does_not_match_unrelated_words` — a decoy term still returns 0 |
| A prefix hit is scored as coverage, not as a miss | `::test_a_prefix_hit_is_not_penalised_into_oblivion`, `::test_coverage_counts_a_prefix_as_present` |
| Hybrid mode (the UI default) benefits too | `::test_hybrid_mode_finds_the_word_form` |
| Quoted phrases stay exact while bare terms flex | `::test_quoted_phrases_stay_exact_while_terms_stay_lenient` |
| The index uses porter stemming | `::test_fts_index_is_built_with_porter_stemming` |
| An existing database is migrated on startup, idempotently | `::test_existing_database_is_migrated_to_stemming` |

Verified against the bug: reverting the tokenizer to `unicode61` fails 4 of
these tests. The production path was checked separately against a real
PostgreSQL 16 instance — `small:*` matches the "smaller" passage and `truck:*`
/ `trucks:*` now both return 2 rows, where `websearch_to_tsquery` returned 0.

## Search: genuine AI semantic retrieval

Semantic search is model-backed, not a lexical trick. With
`sentence-transformers` installed the embedder is
`sentence-transformers/all-MiniLM-L6-v2` (`is_semantic=true`, 384-dim); without
it the app degrades to the `hashing-lexical-v2` lexical fallback
(`is_semantic=false`). The fallback cannot retrieve a concept whose words are
absent — `small` scored ~0.03 cosine against the "smaller" passage and was cut
by the floor. The real model scores it ~0.37 and returns it.

| Property | Evidence |
|---|---|
| A query whose literal words are absent still retrieves the related passage | `test_semantic_real.py::test_real_semantic_finds_concept_without_a_literal_word` — `small` returns the "smaller" passage, `matched_by=[semantic]`, `diagnostics.semantic=true` |
| The `is_semantic` flag is surfaced in diagnostics | `::test_real_semantic_flag_is_surfaced_in_diagnostics` — `embedder` startswith `sentence-transformers`, `semantic=true` |
| The corpus index is in the model's vector space | `scripts/reembed.py` rebuilds all `segment_embeddings` after an embedder change; the demo DB was re-embedded (74 vectors, 384-dim) and an audit event `semantic.reindex` was recorded |

Verified live on the running API (`:8000`): `mode=semantic`, `q=small` returns
the AMI passage *"and then they are much much smaller."* with
`embedder=sentence-transformers` and `semantic=true`; hybrid and keyword also
return it. The two `test_semantic_real.py` tests are skipped automatically on
machines without `sentence-transformers`, so CI without the model still passes.

## Intake: triage versus full index

The PRD assumes every upload is transcribed. That assumption fails on an archive
measured in thousands of hours — a case reopened after ten years needs twenty
minutes, and nobody is paying to index the rest. Triage registers the file
without indexing it, and promotion is explicit. All 18 tests live in
`test_triage_and_custody.py`.

| Property | Evidence |
|---|---|
| Triage registers the file without transcribing it | `::test_triage_registers_the_file_without_transcribing_it` — 201, status `triaged`, no segments |
| It records that it did not transcribe | `::test_triage_records_that_it_did_not_transcribe` — `intake.transcribed is False`, mode `triage`, probe result present |
| The file is still hashed, probed and playable | `::test_triaged_audio_is_still_playable_and_seekable` — `Range: bytes=0-1023` returns `206` + `Content-Range` |
| Triage contributes nothing to search | `::test_a_triaged_recording_contributes_nothing_to_search` — the recording's own words return 0 hits, by design |
| A triaged recording can be promoted later | `::test_a_triaged_recording_can_be_promoted_when_a_case_needs_it` — `/retry` takes it to `ready` with transcription in `processing_meta` |
| Non-audio is still rejected | `::test_triage_still_refuses_a_non_audio_file` — `415` |
| Full intake is unchanged | The other 95 tests upload through the same `_ingest` helper |

Verified live: an 89.95 s file registered as `triaged` with
`intake={"mode":"triage","probe":"ok","transcribed":false}`, provenance showing
exactly two stages, and playback returning
`Content-Range: bytes 0-1023/2878444`. Promotion via `/retry` then produced
`ready` with 28 segments.

## Denoise on demand, and A/B listening

Denoising was an ingest stage — all or nothing, decided before anyone listened.
It is now an explicit action on the Review screen, because a listening aid should
be invoked by the person listening.

| Property | Evidence |
|---|---|
| A derivative can be requested after ingest | `::test_denoise_can_be_run_after_ingest` — 200, `has_denoised is True`, audio served from the derivative variant |
| It never touches the original | `::test_denoising_never_touches_the_original` — source bytes re-hashed after denoising still match the recorded SHA-256 |
| A denoise is a recorded stage | `::test_denoise_is_recorded_in_the_custody_chain` — `recording.denoised` appears in `/provenance` |
| A missing denoiser is reported, not silently skipped | `::test_denoising_with_no_engine_configured_reports_it` — `503` with the reason |
| It is an authorized action | `::test_denoise_requires_a_permission_that_a_viewer_lacks` — viewer gets `403` |
| It refuses to race a running job | `::test_denoise_refuses_to_race_a_job_that_is_still_running` — `409` when a job is queued/running |

Verified on a real file: `engine=deepfilternet-cli`, `version=deep_filter 0.5.6`,
`flags=-D (delay compensated)`, `level_matched_db=0.0`.

The A/B switch preserves position and play state. Verified in the live DOM:
`currentTime` set to 31.5, Denoised clicked, `audio.src` switched to
`...variant=denoised` while `currentTime` stayed `31.5` and the active button
read `Denoised`.

## Chain of custody

The audit log records everything, including 57 playback events on one recording.
A custody chain that shows all of it is unreadable, so `/provenance` is a filtered
projection of the same events.

| Property | Evidence |
|---|---|
| Receipt and completion are both named, with the digest | `::test_custody_chain_names_receipt_and_completion` |
| Steps read oldest-first | `::test_custody_chain_is_ordered_oldest_first` — timestamps monotonic |
| Every step is attributed | `::test_custody_chain_attributes_who_did_the_work` — the intake actor is named, automation is labelled `system` |
| Playback noise is excluded | `::test_custody_chain_hides_playback_noise` — 5 forced plays leave `audio.played` absent and the chain `<= 8` steps |
| A triaged file reads as triaged | `::test_a_triaged_recording_reads_as_triage_in_the_chain` — `intake_mode == "triage"` |
| Promotion shows both halves | `::test_promotion_after_triage_shows_both_halves_of_the_history` — `intake_mode == "triage_promoted"`, triage and completion both present |
| Repeated stages collapse | Live: 13 raw events render as 3 lines, "Segment verified ×11" |

Verified against the bug: removing the allow-list filter fails
`test_custody_chain_hides_playback_noise`; removing the triage branch from
`_ingest` fails 4 tests. Both were re-run after restoring the file.

## Test dataset integrity

The synthetic demo set is what the demo and the ASR baseline rest on, so the
generator that produces it is tested too — it previously had no coverage at all.

| Property | Evidence |
|---|---|
| A transient TTS failure is retried before the voice chain steps down | `test_tts_generators.py::test_transient_failure_is_retried_before_the_chain_steps_down` — 3rd attempt succeeds, provider stays `edge`, `say`/`tone` never called |
| Retries are bounded and exhaustion is reported | `::test_retries_are_bounded_and_the_error_surfaces`, `::test_exhaustion_warns_loudly_about_the_mixed_voice` |
| Only transient failures are retried | `::test_deterministic_providers_are_not_retried` (`say`/`tone` are 1 attempt, 0 sleeps); `::test_absent_dependencies_fail_fast` (`ImportError`/`FileNotFoundError` are not retried) |
| Backoff is exponential, without a pointless sleep after the last attempt | `::test_backoff_is_exponential_and_skips_the_final_attempt` — exactly 2 s + 4 s + 8 s |
| The cache is never confused across providers or voices | `::test_cache_entries_are_keyed_per_provider`, `::test_voice_change_invalidates_the_cache` |
| Mixed-voice builds are detectable after the fact | `::test_sidecar_records_per_line_and_aggregate_provenance` (per-line `tts_provider` + aggregate `tts` block), `test_pipeline.py::test_sidecar_voice_provenance_reaches_the_api` (survives into `Transcript.settings`) |
| The two generators share one retry implementation | `::test_noisy_generator_delegates_to_the_shared_retry`, `::test_noisy_generator_has_no_private_retry_loop_left` |

Verified against the bug: reverting `_synthesise` to call providers directly
fails 3 of these tests, and the captured stderr shows the defect itself —
`line 1: used say after edge (RuntimeError: NoAudioReceived)`.

## Real on-topic audio: the EPS / Technology North press conference

Not a test — a live end-to-end run on a real 30:17 public recording of the subject
the product exists for (<https://youtu.be/qSShW86up-A>). Artefacts, parser,
scoring scripts and the full write-up are in
[`data/real-audio/eps-presser/`](../data/real-audio/eps-presser/README.md).

| Property | Result |
|---|---|
| Ingests through the normal full-intake path | `ready`, **773 segments**, 30:17, denoised derivative produced |
| Wall clock for 30 min of stereo conference audio | 7 min 43 s (CPU int8, `faster-whisper small`, RTF ≈ 0.25) |
| Search finds the passage at the right second | 4/4 queries; verified independently by re-transcribing the hit window |
| Range streaming on a 35 MB original | `206`, `content-range: bytes 0-2047/34983688` |
| Chain of custody | 2 stages: received+hashed (`demo.admin`), transcription+indexing (`system`) |
| Proper nouns beat the caption reference | 110 vs 88 correct; 6 vs 20 mangled |

**The WER against the caption track (20.0% / 18.1%) is reported but must not be
quoted as the error rate.** The reference is YouTube's automatic captions — a
weaker ASR system — so this measures disagreement, and the disagreement is mostly
the reference's fault (`evington`→`edmonton`, `lang hang`→`ling huang`,
`nors uh`→`north`, `ai`→`autism`, `bodyw warn`→`body worn`). Entity counts are
the trustworthy metric here because ground truth comes from the video title, the
channel, and speakers introducing themselves.

**Denoising gets no verdict from this recording.** Whole-file WER favours it by
1.9%; ten 3-minute windows give 3 wins / 2 losses / 5 ties with swings from 11.6%
to 38.6%, and both variants make identical substitutions. The difference is
segmentation variance, not filtering. It remains a listening aid.

## Non-functional checks

| Requirement | Status | Evidence |
|---|---|---|
| Runs locally via Docker Compose | ✅ | `docker-compose.yml`: postgres, qdrant, api, worker, ui (nginx). Compose was **not** executed in this environment (Docker daemon unavailable) — the same code paths were exercised with SQLite + local index and with the async worker |
| GPU acceleration with CPU fallbacks | ✅ | `WHISPER_DEVICE=auto` selects CUDA when `torch` reports it; `compute_type` switches `float16` → `int8` on CPU |
| Long recordings processed asynchronously | ✅ | Job table + `python -m app.worker`; verified by uploading with `PROCESS_INLINE=0` and draining the queue (`processed: 1`, job `succeeded`) |
| Progress and failures reported | ✅ | `jobs.stage` / `jobs.progress` committed per stage; library polls every 1.5 s |
| Originals and transcript history preserved | ✅ | See criteria 2 and 9 |
| Protected audio only for authorized users | ⚠️ PoC | Every route requires a permission (`read`, `upload`, `edit`, `verify`, `audit`); identity is header-based and must be replaced by SSO before deployment |
| Encryption, RBAC, audit logging | ⚠️ Partial | RBAC + audit logging implemented; **encryption at rest / in transit is a deployment item** and deliberately not faked |
| No sensitive audio sent to external services | ✅ | All processing is local. WhisperX downloads model weights on first use; audio never leaves the host. Diarization (optional) requires an HF token but runs locally |

## PRD §13 demo scenario

Reproduced by `test_workflow_e2e.py::test_acceptance_workflow`:

1. Search `blue pickup truck near a community centre` → ≥2 passages from
   different recordings, in **< 2 s** (measured 42–76 ms on the demo corpus).
2. Select the top passage → original audio streamed at its timestamp.
3. Correct a transcription error → status `corrected`, AI text retained.
4. Verify the segment → status `verified`, reviewer + timestamp recorded.
5. Inspect history → revision and audit events for that segment.

**Primary success measure** (locate and review a passage within 30–60 s) is a
human-timing target, not something the test suite can assert. The machine-side
prerequisites are measured: search latency in tens of milliseconds, one click to
seek, one panel for correction and verification.

## Known gaps

- **Accuracy on real speech is much worse than on synthetic speech, and the
  causes are now measured.** On three AMI Meeting Corpus clips (real voices, real
  room mic, human transcripts): **55.4% WER**, against 3.9% on the synthetic
  noisy set. Decomposed with AMI's matched headset-microphone condition —
  identical transcripts, identical timings, only the capture differs:

  | Condition | WER | Content-term recall |
  |---|---|---|
  | Headset mic, one utterance per file | 22.9% | 86.2% |
  | Distant mic, one utterance per file | 46.3% | 66.7% |
  | Distant mic, whole clips | 55.4% | 61.5% |

  The microphone costs ~23 WER points and speaker overlap ~9 more; moving from
  `small` to `medium` recovers only ~3. Three clips is not a benchmark — it is
  enough to identify the dominant error terms and catch regressions, not to quote
  an accuracy figure. See `data/real-audio/README.md`.
- **A real press conference is more encouraging than AMI, and also has no usable
  WER.** On the 30-minute EPS/TN recording the app's transcript beats YouTube's
  automatic captions on every proper noun that matters (110 vs 88 correct
  mentions, 6 vs 20 mangled). But the caption track is another ASR, not a human
  transcript, so there is still **no human-verified ground truth for real,
  on-topic audio** — the single most important missing item. Entity counts and
  independently re-transcribed hit windows are what substitute for it today.
- **The synthetic numbers are optimistic for a structural reason**: their
  reference transcripts were written *from* the script, so the "ground truth"
  and the audio share a vocabulary and a punctuation convention that real exhibits
  never will.
- **Language must stay pinned.** Whisper autodetected English AMI audio as Welsh
  (p=0.73) and emitted gibberish — 77.0% WER, dropping to 40.4% when forced to
  English. `WHISPER_LANGUAGE` now defaults to `en`; `auto` is opt-in.
- **No source separation**, so overlapping speakers remain unrecoverable rather
  than merely hard. Diarization is wired but untested against real multi-speaker
  audio (it needs a Hugging Face token and the WhisperX pipeline), and it would
  not fix overlap even if it worked — it labels a mixed stream, it does not
  separate it.
- **Denoising does not improve transcription on this corpus, and hurts real
  audio.** On the synthetic noisy set it is an exact tie on all six files (after
  fixing a level bug — see below). On the real room-mic clips it is worse on every
  file: +14.7%, +16.3%, +4.1% WER. It is a listening aid, not an accuracy win.
- **A level bug made denoising look worse than it was** (now fixed):
  DeepFilterNet returns ~27% of the input RMS on room-mic audio, and Whisper's
  WER followed the level drop — the same clip scored 95.2% denoised versus 72.8%
  with the level restored and the filtering unchanged. `pipeline/denoise.py` now
  RMS-matches the derivative to the source and records `level_matched_db`. Any
  denoised measurement dated before this fix measured gain, not denoising.
- No load testing; the local cosine index is O(n) per query by design.
- Docker Compose has not been executed here (no daemon). CI should run
  `docker compose up --build` and replay this acceptance list.
