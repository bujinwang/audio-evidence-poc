# Product Requirements Document (PRD)

## AI Audio Evidence Search & Review Platform

| | |
|---|---|
| **Version** | 1.0 |
| **Date** | October 19, 2026 |
| **Status** | Proof of Concept (PoC) |
| **Audience** | Technology North, police departments, investigators, and legal professionals |

---

## 1. Product Overview

Build a demonstration application that transforms audio recordings—including historical cassette recordings, interviews, and body-camera audio—into searchable, timestamped transcripts.

Investigators and lawyers can search recordings using natural language, jump directly to relevant audio passages, listen to the original recording, and correct or verify AI-generated transcripts through a human-review interface.

The solution should integrate existing open-source technologies rather than train new AI models from scratch.

## 2. Objectives

- Reduce the time required to locate relevant passages in long recordings.
- Support keyword, semantic, and hybrid transcript search.
- Provide timestamp-accurate playback of original audio.
- Enable human correction and verification of AI transcripts.
- Maintain traceability between recordings, transcripts, and reviewer actions.
- Demonstrate a modular architecture suitable for future police-sector deployments.

## 3. Target Users

- Police investigators and officers
- Evidence and disclosure specialists
- Lawyers and legal reviewers
- Authorized transcription reviewers and administrators

## 4. User Stories

**Investigator.** As an investigator, I want to search for a description such as "a blue pickup truck near a community centre" so I can find potentially relevant passages without listening to every recording.

**Audio reviewer.** As a reviewer, I want to listen to a passage alongside its transcript so I can correct recognition errors.

**Evidence specialist.** As an evidence specialist, I want to preserve the original recording and retain a history of processing and transcript changes so the workflow remains auditable.

**Legal reviewer.** As a legal reviewer, I want to distinguish AI-generated text from corrected and verified text so I can assess the status of each passage.

## 5. Functional Requirements

### FR-01: Audio Library

- Upload supported audio files.
- Display recording ID, filename, duration, source type, and processing status.
- Show transcript review status.
- Report processing failures and allow authorized retries.

### FR-02: Audio Processing

- Preserve the original audio unchanged.
- Convert working copies using FFmpeg.
- Generate a denoised copy using DeepFilterNet.
- Transcribe both original and denoised audio for comparison.
- Record model versions and processing settings.

Denoising must not be assumed to improve every recording. The original remains the reference recording.

### FR-03: Transcription

- Generate timestamped transcript segments.
- Support word-level timestamps where available.
- Support speaker diarization where technically feasible.
- Retain the original AI-generated transcript.

### FR-04: Search and Indexing

- Support exact-phrase and keyword search.
- Support semantic search using natural-language queries.
- Support hybrid retrieval combining keyword and semantic results.
- Filter by recording ID, date, source type, speaker label, and review status where available.
- Display relevant text passages with timestamps and recording references.
- Provide curated **scenario prompts** (e.g. find a vehicle, a named person, a time or place, an admission) that pre-select the appropriate search mode (keyword / semantic / hybrid), so an investigator starts from an evidentiary ask rather than a blank box.
- Show an explicit **source citation** on each result that jumps to the original audio at the matched timestamp.
- Label which signal produced each hit (keyword, semantic, or hybrid).

### FR-05: Audio Playback

- Allow users to select a search result and jump to its timestamp.
- Play the original recording in context.
- Provide play, pause, seek, and playback-speed controls.
- Synchronize transcript highlighting with playback where feasible.

### FR-06: Human Review

- Display transcript segments beside an audio player.
- Allow authorized reviewers to edit and save transcript text.
- Compare AI-generated text with corrected text.
- Maintain revision history.
- Require an explicit action to mark a segment verified.

**Review statuses:**

1. AI Generated
2. In Review
3. Corrected
4. Verified

A correction must not automatically imply verification.

### FR-07: Audit Trail

- Record reviewer identity and timestamps for edits and verification.
- Preserve prior transcript versions.
- Record relevant processing metadata.
- Maintain an auditable association between each transcript segment and its source recording.

## 6. Recommended Technology Stack

| Layer | Technology | Responsibility |
|---|---|---|
| Frontend | React + TypeScript | Search, playback, and review UI |
| API | Python + FastAPI | Application services |
| Audio conversion | FFmpeg | Format conversion and preprocessing |
| Denoising | DeepFilterNet | Noise reduction |
| Transcription | WhisperX | Speech transcription and timestamps |
| Alternative transcription | Faster-Whisper | Benchmarking alternative |
| Structured database | PostgreSQL | Recordings, transcripts, revisions, metadata |
| Keyword search | PostgreSQL full-text search | Exact and lexical retrieval |
| Vector search | Qdrant | Semantic retrieval |
| Processing | Python background workers | Asynchronous processing jobs |
| Deployment | Docker Compose | Local demonstration environment |

All major processing components should be replaceable through well-defined interfaces.

## 7. System Workflow

1. Upload an authorized audio sample.
2. Preserve the original and record its checksum.
3. Convert a working copy into a supported format.
4. Generate a denoised derivative.
5. Transcribe both original and denoised audio.
6. Store timestamped transcript segments.
7. Index transcript text in PostgreSQL.
8. Generate embeddings and index them in Qdrant.
9. Accept an investigator's natural-language query.
10. Retrieve and rank candidate passages.
11. Display results with source timestamps.
12. Play the original audio at the selected passage.
13. Allow human correction and verification.
14. Retain revision history and audit events.

## 8. User Interface Requirements

### Screen A — Audio Library

- Upload recordings.
- View processing progress and errors.
- Filter and open recordings.

### Screen B — Investigator Search

- Natural-language search field, plus curated **scenario prompts** (find a vehicle, a named person, a time/place, an admission) that each pre-select the matching search mode.
- Keyword, semantic, and hybrid search modes.
- Search filters.
- Ranked results showing passage text, recording ID, timestamps, and review status.
- An explicit **source citation** on each result that jumps to the original audio at the matched timestamp.
- A persistent **"not evidence of fact"** badge on AI-generated matches and on every semantic (similarity) hit, reinforcing that AI relevance is not proof (see §10).
- Audio player that jumps directly to the selected passage.

### Screen C — Transcript Review

- Timestamped transcript beside the audio player.
- Editable transcript segments.
- Original-versus-corrected comparison.
- Review and verification controls.
- Revision history and reviewer attribution.

## 9. Non-Functional Requirements

- Run locally using Docker Compose.
- Support GPU acceleration where available and practical CPU fallbacks.
- Process long recordings asynchronously.
- Report processing progress and failures.
- Preserve original recordings and transcript history.
- Keep protected audio accessible only to authorized users.
- Support encryption, role-based access, and audit logging.
- Avoid sending sensitive recordings to external services by default.

## 10. Privacy and Evidence Handling

- Use synthetic or properly authorized recordings during development.
- Never overwrite the original audio.
- Keep processed audio separate from the source recording.
- Link all transcript passages to source timestamps.
- Preserve AI output and human revisions.
- Treat Alberta and Canadian privacy, retention, disclosure, and evidentiary requirements as deployment-review items.
- Do not automatically assign a real person's identity to a speaker based solely on voice.
- Do not represent AI search relevance or transcription output as proof of factual truth.
- Surface the "AI match is not evidence of fact" principle in the UI as a persistent badge on AI-generated and semantic matches, so the operator is never left to infer the distinction.

## 11. Acceptance Criteria

The demonstration is accepted when:

1. An audio sample can be uploaded successfully.
2. The original remains unchanged.
3. Denoising and transcription jobs execute or clearly report unavailable dependencies.
4. Original and denoised transcripts can be compared.
5. Transcript segments have usable timestamps.
6. Keyword and semantic search return candidate passages.
7. A search result opens the original audio at the relevant timestamp.
8. A reviewer can correct and save a transcript segment.
9. The original AI text and revision history remain available.
10. An authorized reviewer can explicitly mark a segment verified.
11. Review actions are recorded in an audit trail.
12. Automated tests cover the primary workflow.

Search relevance and transcription accuracy must be evaluated against manually reviewed reference material where available.

## 12. Implementation Plan

**Phase 1 — Vertical Slice**
- Audio upload and storage
- Transcription
- PostgreSQL indexing
- Basic search
- Timestamped audio playback

**Phase 2 — Audio Quality**
- DeepFilterNet integration
- Original-versus-denoised transcription comparison
- Processing metadata

**Phase 3 — Semantic Search**
- Embedding generation
- Qdrant integration
- Hybrid search and ranking

**Phase 4 — Human Review**
- Transcript editing
- Verification status
- Revision history and audit trail

**Phase 5 — Demonstration Readiness**
- Docker Compose
- Sample dataset
- Automated tests
- Setup documentation
- End-to-end demonstration

## 13. Demo Scenario

An investigator searches for "blue pickup truck."

The application returns several candidate passages from different recordings. The investigator selects one, listens to the original audio at the relevant timestamp, corrects a transcription error, marks the passage verified, and inspects the revision history.

**Primary success measure:** The investigator can locate and review a potentially relevant passage within 30–60 seconds of initiating a search in the test scenario. This is a target to validate, not a guaranteed result.

## 14. Out of Scope for the Initial Demo

- Training custom speech-recognition models
- Production-scale multi-agency deployment
- Automated determinations of guilt or credibility
- Automated identification of named speakers
- Full production certification or legal compliance validation
- Integration with live police evidence-management systems

## 15. Definition of Done

A locally runnable, end-to-end demonstration that proves audio ingestion, transcription, indexing, semantic search, timestamped playback, human transcript correction, and verification history using synthetic or properly authorized recordings.
