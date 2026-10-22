# Adding AI Capabilities to the Flow & UI — Research Brief

**Status:** Research only. No code changed.
**Date:** 2026-10-22
**Builds on:** `PRD.md` and `docs/TECHNOLOGY_NORTH_GAP_ANALYSIS.md`
**Question:** What can we add to the *flow* and *UI* — inspired by Meta Muse, xAI/Grok Bots, and others — to make the AI in this audio-evidence platform feel native, trustworthy, and useful to investigators, reviewers, and a Technology North buyer?

---

## 0. How to read this

The gap analysis told us **what** to build (redaction cue sheet, paper-and-tape search, disclosure bundle, scale card, QC scorecard). This brief is about the **how** — the *interaction layer* that sits on top of those features. The core idea from the inspirations: stop treating the AI as a **chat box bolted onto the side**, and start treating it as a **colleague that lives inside the case workspace**, leaves auditable artifacts, and is honest about what it can and cannot vouch for.

The app today (from `frontend/src/screens/`): **Cases → Evidence → Find → Review → CueSheet → Bundle → Cost**, plus `PlayerDock`, `CustodyList`, `CoverSheet`, `Stamp`. It is already an evidence-grade *review & custody spine*. The job of the AI layer is to make that spine feel intelligent without ever compromising integrity.

---

## 1. What the inspirations actually prescribe

### 1.1 Meta Muse — "AI as a colleague, not a tool"
Source: Meta design director Alex Cornell interview + Muse interaction teardowns.

| # | Principle | One-line essence |
|---|---|---|
| M1 | Design the AI as a *person*, not a feature | Keep a persistent persona + avatar so the user relates to a "colleague," not a text box. |
| M2 | Progressive disclosure of reasoning | Don't dump chain-of-thought on screen. One-line status; expandable task list for those who want it. |
| M3 | If it's fast, cut UI | Speed removes the need for progress bars, "thinking" spinners, step animations. |
| M4 | Accept non-determinism | Build resilient *frames*, not pixel-perfect screens. You can't control every AI output. |
| M5 | Tell users what the AI can do | Separate "Ideas"/"Feed" surfaces that surface capabilities the user didn't know existed. |
| M6 | No blank input box | Replace the empty search field with *scenario prompts* grounded in the user's real job. |
| M7 | State snapshots, not chat logs | Each action produces a structured, editable, auditable card — not a scrolling thread. |
| M8 | Deliverable-as-interface | The *output* is the next interaction node (inline actions on results). |
| M9 | Honest breakpoints | Refuse clearly, stall progress at ~95%, turn errors into a repair path. Never soft-hallucinate. |

### 1.2 xAI Grok Bots — "Bot, not chat"
Source: xAI design retro (Aug 2026) + first-principles Agent analysis.

| # | Principle | One-line essence |
|---|---|---|
| G1 | Persistent agent with identity + memory | A Bot is a standing entity with its own state, memory, and tools — not a one-shot Q&A. |
| G2 | Avatar status language | idle / working / waiting / blocked / done — the avatar *is* the status report. |
| G3 | Agent = closed loop | goal → state → plan → tool → execute → verify → permission → memory → observe. |
| G4 | Operates on a real workspace | The bot acts *inside* a real environment (here: the case evidence workspace), not on text alone. |
| G5 | Memory + handoff | Remembers context; can save a workflow; multiple bots coordinate and hand off. |
| G6 | Best brain per task | Model is a *swappable component*; route each subtask to the right engine. |

### 1.3 Others worth borrowing
- **Claude Artifacts / Canvas:** the generated artifact is a live, side-by-side, editable object — not a chat message you copy out of.
- **Perplexity citations:** every AI claim carries an inline source link. *This is the single most important pattern for an evidence product.*
- **Cursor plan-mode + background agents:** show a plan before acting; run long jobs in the background; let the user peek and interrupt.
- **NotebookLM:** source-grounded synthesis that is explicitly labeled "draft for human review."

---

## 2. Hard constraints from the domain (these shape every proposal)

From `PRD.md §10, §14` and the gap analysis §5. Any AI-UX idea must pass these:

1. **AI output is never proof of truth.** §10: don't represent AI relevance or transcription as factual proof.
2. **Originals are immutable.** Never overwrite audio; keep AI text separate from human corrections (`ai_text` immutable, revisions append-only).
3. **No speaker identification / naming.** Out of scope; legal landmine in disclosure.
4. **No automated redaction (actual blurring).** Pre-mark only — don't compete with TN's operators.
5. **No emotion / stress detection.** Ethical line; use exposure accounting instead.
6. **Human-in-the-loop is mandatory.** Evidence actions need an explicit reviewer decision + audit event.
7. **Everything is auditable & reversible.** The AI's memory and actions must be traceable in the custody view.

> The Grok "autonomous bot on a real computer" pattern is **dangerous here** and must be deliberately constrained: the AI may *propose and prepare* inside the workspace, but it may not *commit* evidence actions without a human go-ahead.

---

## 3. Mapping: where each pattern lands in the current flow

| Screen | Current state | AI-native proposal | Inspired by |
|---|---|---|---|
| **Cases** | List of cases | Case assistant "remembers" the investigation focus; suggests next actions per case | M1, G1, M5 |
| **Evidence / Library** | Upload, status, triage | Status glyph per recording: idle / transcribing / indexing / blocked (needs decision). Confidence-aware promotion | G2, M2, H |
| **Find** | Natural-language box + keyword/semantic/hybrid | Replace blank box with *scenario prompts* ("find a vehicle," "find a named person," "find a time/location") + clear AI-vs-keyword labels | M6 |
| **Review** | Editable segments, verify | Inline "not evidence" badge + citations on every AI span; AI proposes corrections, human accepts | M9, Perplexity, M8 |
| **CueSheet** | (new) redaction prep | AI pre-marks PII spans as a *live, editable, reason-coded* list — deliverable is the interface | M8, Artifacts, Gap A |
| **Bundle** | Export ZIP | AI assembles a *draft* disclosure package; human approves before finalize | G3, M7 |
| **Cost / Scale** | Metrics | Show "model card" per action (which engine did this) + the cost arithmetic | G6, Gap D/G |

---

## 4. Concrete, prioritized proposals

### A. Persistent "Case Clerk" persona + status (M1, G1, G2)
Give the AI one consistent identity visible across all screens — e.g., a small "Clerk" glyph in the `Layout` header. Its avatar *is* the status: `idle · transcribing · indexing · searching · blocked · done`. This replaces the ad-hoc "AI" labels and gives investigators a stable thing to relate to. Memory is **scoped to the case and fully logged** (never personal profiling) — it remembers the investigation's focus and the reviewer's standing decisions.

### B. State-snapshot cards instead of a chat log (M7)
When the AI does work (transcribe, detect identifiers, summarize a recording), it emits a **snapshot card**, not a chat message:
```
[2026-10-22 14:22] Recording EV-041 · redaction prep
  Input:   EV-041.wav (47:12)
  Did:     scanned 1,204 segments for identifiers
  Found:   38 candidate spans (names ×12, plate-like ×4, addresses ×9, …)
  Conf:    medium–high
  Next:    open CueSheet to accept/reject
```
Editable summary fields + a custody entry. This is Muse's pattern made *evidentiary*.

### C. Intention-guided Find (M6)
Replace the empty search field with 3–4 scenario prompts drawn from the investigator's actual job, each tagged with its search mode:
- "Find a vehicle description" → semantic
- "Find a named person mentioned" → NER + keyword
- "Find a time / location reference" → hybrid
- "Find an admission or confession" → semantic (clearly labeled *heuristic, not proof*)

Manages expectation, kills blank-page anxiety, and teaches users what's searchable. Keep a free-text fallback.

### D. Deliverable-as-interface (M8, Artifacts)
The cue sheet, the disclosure bundle, and the chain-of-custody page become **live, interactive objects**, not download-only files. The cue sheet is where the AI pre-marks spans and the reviewer accepts/rejects inline (Gap A). The bundle is assembled as a *draft* the human approves (Gap C). Every generated object carries actions on itself.

### E. "Ideas" capability-discovery panel (M5)
A contextual side panel that, from the current case state, surfaces capabilities the user may not know exist:
- "3 segments have overlapping speakers — flag them?" (Gap H)
- "12 segments are low-confidence — review those first?" (Gap H)
- "This case has a scanned PDF — search it with the audio?" (Gap B)
This doubles as a soft nudge toward the gap-analysis features and solves Muse's "users don't know what the AI can do" problem.

### F. Inline citations on every AI claim (Perplexity)
The most important pattern for this domain. Every AI-generated span — a summary sentence, a detected identifier, a semantic match — links to the **exact source segment / timestamp / document page**. No claim without a clickable provenance anchor. This is the UX embodiment of PRD §10.

### G. Plan-then-act agent loop with human go-ahead (G3, Cursor)
For a multi-step request ("find everything about the blue truck across audio + docs"), the AI first shows a **plan**: search transcripts → search documents → check cue sheet → assemble clip reel. It executes via the *existing* endpoints (search/transcribe/denoise/bundle), verifies, then hands off a draft for human verification. The plan is shown and requires a go-ahead — evidence-grade human-in-the-loop, Grok-style loop.

### H. Confidence & error-budget UI (M9 + Gap H)
Every AI element shows a confidence indicator. The review queue can sort by *lowest confidence first*, and the UI can report: *"reviewing the lowest-confidence 12% of segments reached 98% term accuracy."* This is the evidence-domain version of Muse's honesty — make the AI's uncertainty a first-class, useful control.

### I. Honest breakpoints / "not evidence" badges (M9)
A persistent, calm badge on every AI-generated element: *"AI-generated · not evidence of fact."* When the AI can't do something (identify a speaker, verify a claim), it says so plainly with a repair path — never a soft answer. This turns PRD §10 from a written rule into a visible, reassuring UI pattern.

### J. Multi-model orchestration (G6) — mostly architecture, lightly visible
Route each subtask to the right engine (WhisperX for speech, an NER model for identifiers, an LLM for summarization). The PRD already says components are swappable. UX benefit: a **"model card"** in the custody/audit view showing *which engine + version* produced each artifact — credibility for a police buyer.

### K. Background agent execution + interruptible status (Cursor/Grok)
Long jobs (denoise, batch index, batch redaction-prep) run in the background behind the status glyph (A). The user can peek at a collapsible task list and interrupt — no blocking modals, no fake progress spinners (M3).

---

## 5. What we should NOT build (and why)

| Tempting | Why not |
|---|---|
| Autonomous evidence actions (Grok "bot on a real computer") | Evidence integrity requires human commitment; constrain to propose/prepare only. |
| Automated redaction / blurring | Gap analysis: don't compete with TN's operators; pre-mark only. |
| Speaker identification / naming | PRD out-of-scope; disclosure legal landmine. |
| Emotion / stress detection | Ethical line (Gap F); use exposure accounting. |
| Representing AI output as proof | PRD §10; use "not evidence" badges instead. |

---

## 6. Demo narrative — how the AI layer reinforces the TN pitch

The gap analysis's one-liner: *the app must become "the review layer under both of TN's businesses," not "an audio tool."* The AI layer supports that exact pitch:

1. **"This is a colleague, not a chatbot."** The Case Clerk persona + status shows you understand *workflow*, not *features*.
2. **"It knows what it can and can't vouch for."** Citations + "not evidence" badges = evidentiary discipline a Crown reviewer trusts.
3. **"It prepares, you decide."** Plan-then-act + draft bundles = human-in-the-loop disclosure, the thing buyers actually buy.
4. **"It remembers the case."** Persistent, auditable memory = the review spine that both the redaction line and the digitization line plug into.

---

## 7. Open questions / next steps (for you to decide)

1. **Persona naming & tone** — "Case Clerk"? Something less subservient? This is a product/brand call for TN.
2. **First slice to prototype** — my recommendation: **C (intention-guided Find) + F (inline citations) + I ("not evidence" badge)**. Lowest build cost, highest credibility gain, and they sit on screens you already have.
3. **Where the "Ideas" panel lives** — docked right rail vs. a collapsible drawer? Depends on screen real estate in `Find`/`Review`.
4. **Memory scope** — confirm case-scoped (not cross-case, not personal) to keep the audit story clean.

No code has been touched. When you're ready, pick a slice and we can design the component-level spec.
