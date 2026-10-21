# UI/UX redesign — handoff

**Prepared for:** Bert Wang
**Audience for the work:** Technology North (Ling Huang and team), and the police
clients who will sit in the room after them
**Status:** design direction + clickable prototype + implementation plan
**Prototype:** `docs/redesign/prototype.html` (open in a browser, no build step)
**Screens:** `docs/redesign/screens/01-cases.png` … `07-cost.png`

---

## 1. The finding that drives everything

The current UI is a **search application**. Technology North does not sell search.
They sell **disclosure work**: bodycam redaction for Edmonton Police Service and
paper digitization, staffed by a neurodiverse team, billed per unit of work and
defended against a disclosure deadline.

That mismatch is the whole redesign brief. A prettier search screen would still
answer the wrong question. What follows re-frames the product as an **operations
console for evidence**: cases, exhibits, custody, cue sheets, bundles — with search
as one instrument inside it.

### What the research says

| Source | What it establishes |
|---|---|
| `technologynorth.net` | "Augment your Staff with Inclusive Productivity" — body cam redaction, document digitization, staff augmentation. Award-winning autism employment programme. "Meticulous attention to detail", "precision and security". |
| `technologynorth.net/redaction` | Eight service lines including **Audio Redaction**. "Meet the latest evidence-handling protocols." Native integration with **Axon**. "We prioritize performance and quality by rigorously measuring every aspect of our services." Specialists hold **enhanced security clearance**. |
| EPS press conference (Dec 2024) | 280 cameras today, **~1,400** across the service. Redaction is exhaustive and frame-by-frame, and it is **audio too**. Complexity triage already exists as a business process (low/medium/high; use-of-force never leaves EPS's queue). AI pre-screening is planned. **RoboCoach**: a red button that blanks the screen and calls a coach. 27 staff → 50 → 100. |
| "Life of a box" (digitization line) | Five stations: preparation → scanning → verification → repacking → QC. Custody recorded **per task, per file, per station**. Every station QAs the previous one. QR cover pages. 150 MB PDF split. OCR output, six-month retention. |
| Ling Huang's email (Oct 19, 2026) | 100+ hours of 911 audio per day. Microcassette going back decades. A reopened case needs **20 minutes out of thousands of hours**. "The police is not going to spend a million dollars to digitize and do a full index." |
| `docs/TECHNOLOGY_NORTH_GAP_ANALYSIS.md` | The scope gap in detail, and the ranked list of artifacts worth building. This redesign implements the UI half of items A, C and D. |

Three sentences from that research shape the design:

1. **"We record who performed which task upon each file and capture when each file
   was processed at every stage."** → Custody is not a tab. It is a permanent
   column in every list, and a visible strip on every screen.
2. **"The police is not going to spend a million dollars to do a full index."** →
   Cost and coverage are first-class UI, not a footnote. The app must show what is
   indexed, what is dark, and what indexing one more minute would cost.
3. **"Meticulous attention to detail"** is their product claim. → The interface has
   to look meticulous. Dense, aligned, tabular, unhurried. Every number the same
   width, every timecode in the same column.

---

## 2. What is wrong with the current UI

Not taste — these are specific failures against the audience.

| # | Problem | Why it matters to TN |
|---|---|---|
| 1 | **No case.** Three tabs (Search / Library / Review) with no case number, no exhibit, no disclosure clock. | Every TN workflow is a case file with a deadline. The app has no idea what a case is. |
| 2 | **Search is the front door.** The app opens on a query box and pre-runs a demo query. | Their operators do not start with a question. They start with a queue of files. |
| 3 | **Floating cards, soft shadows, pastel pills.** Reads as a 2024 SaaS product. | A police buyer is deciding whether this can hold evidence. It should look like a records office. |
| 4 | **Custody is buried.** It appears only when you press "Chain of custody" inside a recording. | Custody is the product. It should be visible in the list, not one click away. |
| 5 | **Indexing is binary.** A file is indexed or it is not. | Ling's entire economics is partial indexing: 20 minutes out of thousands of hours. Window promotion is invisible today. |
| 6 | **No artifacts.** The app produces a transcript and stops. | TN delivers worksheets, cue sheets and bundles. Nothing here can leave the building. |
| 7 | **Status language is decorative.** Pastel "AI generated / Corrected / Verified" pills of equal visual weight. | These are evidentiary states with different consequences. They should read as stamps on a file, not tags on a card. |
| 8 | **No density control and 14px text.** Rows are 60px tall; a 1,204-file case would need 72 screens of scrolling. | Their staff review for hours. Density is an accessibility feature, not a preference. |

---

## 3. Design principles

1. **The case is the frame.** Nothing exists outside a case. Case number, exhibit,
   and disclosure date are always on screen — the same way a paper file carries its
   number on every page.
2. **Custody is furniture, not a feature.** A custody count in every row, a custody
   strip on every screen, an append-only event list wherever a decision is made.
3. **Show the dark.** Unindexed minutes are rendered explicitly ("37 m dark"), not
   hidden. What you cannot search is as important as what you can.
4. **Stamps, not pills.** Status is a rectangular, letter-spaced, bordered stamp —
   the visual grammar of a file that has been processed, not a badge that has been
   gamified.
5. **One accent, no decoration.** Navy for action and selection. Colour is reserved
   for state (green verified, amber open, red at risk). No gradients, no illustration,
   no icon soup.
6. **Numbers behave.** Tabular figures everywhere; timecodes, hashes and case
   numbers in mono; decimal points aligned. If a reviewer cannot scan a column of
   times, the interface has failed.
7. **Calm under load.** No animation beyond a 120 ms state change. No modals for
   routine work. No content that moves while you are reading it.
8. **Every screen has an exit artifact.** Find ends in a review queue; review ends
   in a cue sheet; cue sheet ends in a bundle. Nothing is a dead end.

---

## 4. Information architecture

```
Case queue                      ← the front door, sorted by disclosure pressure
└── Case (26-104772)
    ├── Evidence                ← exhibits, coverage, review state, custody
    ├── Find                    ← one query across the case, results are passages
    ├── Review                  ← transcript + correction + cue candidates
    ├── Cue sheet               ← redaction decisions, exportable EDL
    └── Bundle                  ← what goes to the Crown, with manifest
Cost model                      ← archive economics, for the buyer's CFO
```

Navigation is a persistent left rail (196 px) with three groups: **Case**,
**Disclosure**, **Planning**. The top bar carries the case breadcrumb, the
disclosure clock, and the acting user — the three things that must never scroll
away. There is no global search box; search belongs to a case, and `/` opens it
from anywhere.

Compared with today: **Search → Find** (same capability, case-scoped, table
instead of cards), **Library → Evidence** (case-scoped, plus coverage and
custody), **Review → Review** (three panes, cue candidates inline). Three screens
are new: **Case queue**, **Cue sheet**, **Bundle**. One is new for the buyer:
**Cost model**.

---

## 5. Screen specs

### 5.1 Case queue — `screens/01-cases.png`

**Purpose:** answer "what is due, and what is stuck" in five seconds.

- **Page head:** title, one-sentence explanation of the sort order, filter chips.
- **Meta strip:** open cases, exhibits, hours of audio, disclosure at risk,
  reviewer capacity. Five numbers, no charts.
- **Table:** case, subject + one line of context, exhibits, stage, owner,
  disclosure. Disclosure column is the only coloured text — red under 7 days.
- **Two supporting panels:** *Disclosure clock* (progress bars against deadline)
  and *What needs a person* (a short, honest to-do list of exhibits).
- **States:** empty (no cases → "Register a case" with a one-line explanation);
  loading (skeleton rows at fixed height, no spinner).

> Design note: the queue sorts by disclosure pressure, not recency. That single
> decision tells a police audience the software was built by someone who has missed
> a deadline.

### 5.2 Evidence — `screens/02-evidence.png`

**Purpose:** the exhibit list for one case, and the place where partial indexing
becomes legible.

- **Case header:** serif case title, case number, disclosure stamp, one paragraph
  of context, four actions (Promote a window / Continue review / Build disclosure /
  Case custody record).
- **Meta strip:** exhibits, audio hours, indexed vs total, reviewed %, cue
  candidates, custody events.
- **Table columns:** exhibit, recording + provenance line, source, length,
  **Indexed** (Full / Window with the exact range and dark minutes / None), Reviewed
  (bar + %), Custody (event count), action.
- **Explanation panel:** "Why E-002 is only windowed" — three numbers (full index,
  this window, still dark) and the disclosure consequence. This is Ling's argument
  rendered as UI.
- **Custody panel:** the five most recent events for the case, newest first.

**Window promotion** is the interaction to protect in implementation: a modal that
asks for a time range, shows the cost delta ("4 m instead of 41 m · 37 m stays
dark"), and writes a custody event. It must never be a checkbox.

### 5.3 Find — `screens/03-find.png`

**Purpose:** retrieve a passage, then get out of the way.

- Query bar with `/` hint, scope chips (This case / Whole archive), mode segmented
  control (Hybrid / Keyword / Semantic).
- Results as a **table**, not cards: rank gutter, timecode, exhibit, passage +
  provenance line, retrieval signals (`SEM` / `KW`), review status, score.
- Selected row is tinted and marked with a 2 px left rule.
- **Player dock**, fixed to the bottom: title, passage range, waveform, position,
  ±5 s, rate, *Open in review*. It survives navigation within the case.

> Design note: the dock is the only element with a shadow in the entire product.
> That is deliberate — it is the one thing that floats above the page.

### 5.4 Review — `screens/04-review.png`

**Purpose:** the only screen where a human changes evidence. Three panes, one job.

- **Left (290 px):** segment list sorted **lowest confidence first**, with a
  confidence chip per row (red < 80, amber < 92, green ≥ 92). Clicking seeks the
  audio.
- **Centre:** the segment at 19 px serif — large enough to read aloud against the
  audio. Low-confidence words carry an amber underline. Actions: Correct text,
  Play passage, Compare denoised.
- **Correction panel:** machine text vs current text as a diff, with `mark` on the
  changed token and a required reason field. The reason is written to the revision
  log; it is not optional in the real build.
- **History:** append-only, two rows in the prototype, each with actor and time.
- **Right (316 px):** *Redaction* — cue candidates for this segment with entity,
  reason code, confidence, and three decisions (Accept / Reject / Not redactable);
  and *Custody* — the event list for this exhibit.

The right rail is the redesign's main argument: **redaction happens during review**,
not in a separate tool afterwards.

### 5.5 Cue sheet — `screens/05-redaction.png`

**Purpose:** the artifact TN's operators actually hand off.

- Header actions: Export CSV, Export JSON, Send to redaction queue.
- Meta strip: candidates, accepted, rejected, open, exhibits covered.
- Table: start, end, span text, entity type, **reason code**, confidence, decision,
  reviewer. Reason codes are visible strings (`PII-MINOR`, `PII-ADDR`,
  `MED-INFO`, `NOT-REDACT`), not an enum hidden in a settings page.
- Footer: the export schema, spelled out, with "EDL-compatible · one row per span".

> Design note: the app does not blur anything. It produces the worksheet that
> drives the blur pass in TN's existing tool. Competing with the customer's core
> competency would read as a misunderstanding of their business.

### 5.6 Bundle — `screens/06-disclosure.png`

**Purpose:** convert "we have a search tool" into "we have a disclosure workflow".

Three columns: **Contents** (checklist with sizes and a warning that names the
unverified exhibits), **Cover sheet** (a rendered document, serif, with case,
exhibits, preparer, review state, manifest hash), **Manifest** (file → sha256,
plus the build button).

The warning is the feature: *"2 exhibits unverified. The bundle can be built, but
the cover sheet will say so."* A tool that refuses to hide its own uncertainty is
the one a Crown attorney trusts.

### 5.7 Cost model — `screens/07-cost.png`

**Purpose:** the page for the person who signs.

- One table, three approaches to the same question (full index / windowed / spotting
  pass), with compute, cost, index coverage and time-to-first-answer.
- "What this buys" — the arithmetic in sentences, at $/audio-hour.
- **"Where it breaks"** — a short, honest list: spotting recall measured on 40
  tapes not 3,900; overlap defeats the heuristic; p95 above 1M segments not measured;
  denoising skipped on triage because it costs more than transcription.

> Design note: the "where it breaks" panel is not modesty. It is the slide that
> survives a technical audience, and it is cheaper to build than another feature.

---

## 6. Design system

### 6.1 Tokens

```css
/* surface */
--paper:#f5f4f0;      /* page */
--surface:#ffffff;    /* panels, tables */
--surface-2:#faf9f6;  /* hover, insets */
--rule:#e1ded6;       /* hairline */
--rule-2:#cac6bb;     /* panel border */

/* ink */
--ink:#14171b;  --ink-2:#474d55;  --ink-3:#666e79;

/* one accent */
--navy:#12365c;  --navy-2:#0c2745;  --wash:#e8edf3;

/* state — never used for decoration */
--green:#1a5a38; --green-w:#e6f0e9;   /* verified, accept */
--amber:#7a4a05; --amber-w:#f7edda;   /* open, windowed, caution */
--red:#8a2222;   --red-w:#f6e6e5;     /* at risk, low confidence */
--grey:#4b525b;  --grey-w:#ebedf0;    /* unreviewed, neutral */

--radius:3px;                         /* 2px on stamps, 12px only on chips */
```

Deliberate choices: the page is warm off-white (`#f5f4f0`) rather than blue-grey,
because evidence software sits next to paper all day. Panels are separated by a
1 px warm rule, not a shadow. There is exactly one accent, and it is a dark
institutional navy rather than a product blue.

### 6.2 Type

| Role | Face | Size | Notes |
|---|---|---|---|
| UI | system stack | 13 px / 1.45 | base; 12.5 px in tables |
| Case titles | Georgia / serif | 20–22 px | the only serif in the chrome; gives the file a cover page |
| Segment text | Georgia / serif | 19 px | read aloud against audio |
| Labels | system, uppercase | 9.5 px, `.09em` | table heads, meta keys |
| Numerals | tabular | inherited | `font-variant-numeric: tabular-nums` on `body` |
| Time, hash, case no. | `ui-monospace` | 11–11.5 px | never proportional |

### 6.3 Components to build

| Component | Replaces | Notes |
|---|---|---|
| `<Stamp status>` | `.badge` | rectangular, uppercase, bordered; 6 states |
| `<MetaStrip items>` | — | key/value band under a page head |
| `<DataTable>` | ad-hoc tables | sticky head, tabular numerals, row hint on hover, keyboard row focus |
| `<CaseHeader>` | — | serif title + case no. + disclosure stamp + actions |
| `<PlayerDock>` | `<AudioPlayer>` card | fixed bottom bar; waveform, ±5 s, rate, variant toggle |
| `<CustodyList>` | inline `<ol>` | append-only events with connecting rule |
| `<ConfidenceChip>` | — | 3 thresholds, colour + number |
| `<CueRow>` | — | entity, reason code, confidence, 3 decisions |
| `<CoverSheet>` | — | print-styled document block |
| `<ManifestList>` | — | filename + sha256, monospace |
| `<Kbd>` | — | keyboard hints |

### 6.4 Keyboard

`/` search · `j` / `k` next/prev result · `↵` open · `Esc` back to queue ·
`space` play/pause in the dock · `v` verify · `c` correct. Show the hints in the
UI (`.kbd`), because the reviewers are keyboard-first and the hints teach the model.

---

## 7. Accessibility and the neurodiverse workforce

TN's staff are the product. The interface is the tool they hold for six hours at a
time, so accessibility is a functional requirement, not a compliance checkbox.

- **WCAG 2.2 AA** as the floor: 4.5:1 text contrast (the palette above is checked),
  3:1 for UI borders, visible 2 px focus rings with 1 px offset, no colour-only
  signalling (every stamp has a word).
- **Predictable layout.** The rail never moves, the meta strip never moves, tables
  keep their column order. No content reflows on hover.
- **No motion** beyond 120 ms opacity/colour. `prefers-reduced-motion` respected.
- **Density toggle** (comfortable / compact) in the rail footer: 32 px vs 26 px
  rows. Both are readable; neither is cramped.
- **No modals for routine work.** Modals are reserved for window promotion and
  bundle build — actions with a cost.
- **Session exposure** (from the gap analysis, item F): count minutes of flagged
  content and continuous session length, and offer a break prompt. Explicitly *not*
  emotion detection — that would be surveillance of a vulnerable workforce, and it
  is the one feature that would make the room uncomfortable.
- **Plain language.** "Window", "dark minutes", "cue sheet", "manifest" are TN's
  words. Avoid "AI-powered", "smart", "insights".

---

## 8. Implementation plan

The prototype is static HTML. The React app keeps its architecture; this is a
re-skin plus three new screens. Estimates are for one developer familiar with the
codebase.

### Phase 0 — design system (1.5 days)

| File | Change |
|---|---|
| `frontend/src/styles/tokens.css` | new — the token block above |
| `frontend/src/styles/base.css` | new — reset, type scale, focus rings |
| `frontend/src/styles/components.css` | new — stamp, table, strip, panel, dock |
| `frontend/src/styles.css` | reduced to imports; old rules deleted, not overridden |
| `frontend/src/components/` | `Stamp`, `MetaStrip`, `DataTable`, `Kbd`, `Panel` |

Delete the old `.badge--*` and `.tag--*` rules rather than layering on top. Two
visual languages in one stylesheet is how redesigns rot.

### Phase 1 — shell and existing screens (3 days)

| File | Change |
|---|---|
| `frontend/src/App.tsx` | rail + topbar + `CaseContext`; hash routes `#/case/:id/evidence` etc. |
| `frontend/src/screens/Cases.tsx` | new — queue table |
| `frontend/src/screens/Evidence.tsx` | from `Library.tsx`; add indexed/reviewed/custody columns, case header |
| `frontend/src/screens/Find.tsx` | from `Search.tsx`; table results, `PlayerDock` |
| `frontend/src/screens/Review.tsx` | three-pane layout, confidence sort, diff editor |
| `frontend/src/components/PlayerDock.tsx` | from `AudioPlayer.tsx`; fixed bar, waveform from peaks |

Backend: a `cases` table (`id, number, subject, disclosure_due, owner`) and
`recordings.case_id`. Migration is additive; existing recordings get a seeded case.

### Phase 2 — cue sheet (3 days)

- New `cue_candidates` table: `segment_id, start_s, end_s, span_text, entity_type,
  reason_code, confidence, decision, reviewer, decided_at`.
- Detector: regex for plates/phones/dates + a name list; store candidates on ingest.
  No model training. Label it heuristic in the UI.
- `GET /api/recordings/{id}/cues`, `POST /api/cues/{id}/decision`,
  `GET /api/cases/{id}/cues.csv|json`.
- Screen: `Redaction.tsx`, plus the aside in Review.

### Phase 3 — bundle and cost (2.5 days)

- `POST /api/cases/{id}/bundle` → ZIP with clips (existing `/clip` endpoint),
  transcripts, revision log, custody PDF, cue sheet, `manifest.sha256`.
- Cover sheet rendered server-side to PDF (one template).
- `GET /api/cases/{id}/cost` → the three-scenario table from measured RTF.
- Screens: `Disclosure.tsx`, `Cost.tsx`.

**Total: ~10 working days** for one developer, or 6 calendar days for two with
phase 0 and 1 in parallel after the tokens land.

### What must not be faked

The prototype shows numbers that the app does not yet compute — hours indexed,
dark minutes, cue counts, bundle sizes. In implementation, every one of those must
come from a query or be visibly labelled as an estimate. A demo that invents a
number is worse than a demo that omits it, and this audience will ask.

---

## 9. Deliberately not built

| Tempting | Why not |
|---|---|
| Emotion / stress detection for reviewers | Unreliable, and surveillance of the people the programme exists to protect. Exposure accounting gets the same benefit honestly. |
| Automatic blurring | That is TN's core competency and their QC chain. Pre-marking is the right altitude. |
| Speaker identification | Out of scope by PRD §14, and a legal landmine in disclosure. Overlap flags instead. |
| A video player with frame annotation | Four weeks to be worse than the tool they already own. Stay on the review-and-custody spine. |
| Dark mode | Not before the light theme is right. A second palette doubles the contrast testing for no buyer. |
| Onboarding tour | The empty states and the keyboard hints teach the model. Tours are read once and skipped forever. |
| ~~A threshold that makes "no match" return nothing~~ **Shipped** | Four approaches were attempted and rejected in an earlier pass — see §14 — because cosine has no natural zero and every *flat* constant broke a real query on some corpus. It shipped once the threshold was made *corpus-scaled* instead of flat: the floor a score must clear depends on how many vectors were ranked, so it means "how extreme is this for a corpus this size". All six nonsense queries now return 0 rows in every mode, and the eight genuine queries with margin are unchanged. Two queries (`helicopter`, `presser`) also return nothing, which is correct: neither word is in the transcript, and the row `helicopter` used to return was a spurious max at 0.3584 while every near-synonym still resolves the passage. A calibrated constant, not a guarantee. |

---

## 10. How to show it (45 minutes)

Adapted from the gap analysis, now UI-led:

1. **2 min — their numbers.** 100 hours a day, 20 minutes needed, ten years later,
   no million dollars. Do not open the app yet.
2. **4 min — case queue.** "Sorted by disclosure pressure, not recency." Point at
   the two cases at risk.
3. **8 min — Evidence.** E-002: 30 minutes, fully indexed, 5 cue candidates open.
   Show the cost of the window vs the full index. This is Ling's email, answered on
   one screen.
4. **8 min — Find and Review.** `woman hiding from an attacker` → the passage →
   the player dock → correct → verify. Keep it under three minutes of clicking.
5. **10 min — Cue sheet.** Accept two candidates, reject one as evidential, export
   the CSV. Say out loud: "we do not blur anything; this drives your blur pass."
6. **5 min — Bundle.** Open the cover sheet, point at the warning about unverified
   exhibits, show the manifest.
7. **5 min — Cost model and the honest page.** End on "where it breaks".
8. **3 min — the question.** "Which of these is worth a pilot?" Then stop talking.

**If a stall happens mid-demo.** The job-recovery endpoints (`POST
/api/jobs/{id}/reset`) exist for exactly this, and three properties are worth
knowing before you reach for one:

- **A reset starts no work.** It retires the job row and writes one audit event.
  It does not re-index, re-denoise, or enqueue anything, so using it does not
  commit you to a wait you did not plan for.
- **A reset cancels nothing.** There is no cancellation mechanism, so a live
  worker keeps running and may still write its own audit events afterwards.
  `failed`/`reset` records *that an operator gave up*, not *that the run
  stopped* — which is why the `job.reset` event snapshots the stage, progress,
  `started_at` and attempt count observed at that moment.
- **A reset does not preserve the transcript.** The reset itself leaves
  transcripts and segments in place. But the retry it unblocks calls
  `_clear_previous_attempt()` before the first stage, so it re-transcribes from
  scratch — and if that attempt fails, or ASR is unavailable, the exhibit ends
  with no segments at all. Reset a `processing` recording only if you are
  prepared to redo the transcription. On this demo that is minutes, on a
  two-hour cassette it is not.

  There is no safety net here, so treat this as a real hazard rather than a
  theoretical one. `review_locked()` refuses a retry on a recording that has
  human review — but **the delivered demo has none**: all 1865 segments are
  `ai_generated` at `version = 1`, so the guard does not fire and a retry is
  accepted. The loss is reachable on any of the six recordings from the
  starting state of this database, without anyone having reviewed anything
  first. The guard only starts protecting once a segment is touched.

---

## 11. Open questions for Ling

1. **Case as the unit?** The redesign assumes every exhibit belongs to a case with
   a disclosure date. If TN's intake is batch-per-box rather than case-per-file, the
   queue should be boxes, and the case becomes a grouping inside it.
2. **Reason codes.** I used `PII-MINOR`, `PII-ADDR`, `MED-INFO`, `NOT-REDACT`. TN
   and EPS have their own taxonomy; the UI should render whatever list they use,
   not invent one.
3. **Who verifies?** The prototype assumes a reviewer can verify their own
   correction. Their station-to-station model suggests verification should be a
   second person. If so, Review needs an assignment step.
4. **Where does the cue sheet land?** If their redaction tool cannot consume an EDL
   today, the export format matters more than the screen.
5. **Brand.** This is deliberately TN-neutral in chrome and uses their name only in
   the top bar and cover sheet. If it is shown as a TN product, the header should
   carry their mark and their palette.

---

## 12. Files

```
docs/redesign/
├── README.md            ← this document
├── prototype.html       ← clickable prototype, no build step
└── screens/             ← 1440×900 captures of all seven views
```

---

## 13. What shipped (build notes)

The prototype was built out in the React app. Everything below is live and reading
real data; this section records where the build deliberately differs from the
prototype, so nobody has to reverse-engineer the difference.

### Shipped

| Area | State |
|---|---|
| Design system | `frontend/src/styles/{tokens,base,components,screens}.css`; the old 1,400-line stylesheet is gone, not overridden |
| Shell | Persistent rail + top bar with case breadcrumb, disclosure clock and acting role; hash routing (`#/case/<id>/<view>`) |
| Case queue | Live: two cases seeded from the real recordings, sorted by disclosure pressure |
| Evidence | Live: exhibit order follows recording date, indexed/dark/reviewed/custody per exhibit, window-promotion copy, upload into a case |
| Find | Live: case-scoped search, results as a table, player dock with a clickable waveform |
| Review | Live: three panes, correction with reason, verification, history, cue decisions inline |
| Cue sheet | Live: 21 rule-detected candidates on the seeded library, accept/reject/not-redactable, CSV + JSON export |
| Bundle | Live: ZIP with audio, transcripts, custody JSON, cue sheet, cover sheet PDF and a `manifest.sha256` that verifies with `sha256sum -c` |
| Cost model | Live: three scenarios for the case and for a parameterised archive |
| Backend | `cases` + `cue_candidates` tables (additive migration), rule detector in `app/cues.py`, bundle builder in `app/bundle.py`, `routers/cases.py` |
| Tests | 196 passing, including 12 for cases, cues, cost and the bundle manifest |

### Deliberate deviations

1. **No confidence chips in Review.** The prototype sorted segments by ASR
   confidence. The pipeline does not store word- or segment-level confidence, so
   showing a number would have been invented. Review instead shows review state per
   segment and a **Needs review** filter. Storing `avg_logprob` at ingest is the
   prerequisite for the original design.
2. **No window promotion yet.** Indexing is `full` or `none`; a triaged exhibit is
   dark in its entirety. The Evidence screen tells that truth rather than faking a
   partial range. Promoting a time window is the next slice, and the UI already has
   the vocabulary for it.
3. **Case custody shows stage-level provenance**, not the raw audit stream. The
   `provenance` projection already existed; a case-level custody feed is a small
   endpoint away.
4. **Cost is `audio hours × rate`**, not `compute hours × rate`. The prototype's
   arithmetic would have under-priced the archive by 6×. The compute figure is
   shown separately because it is what the infrastructure has to do.
5. **Bundle cover sheet is a real PDF** (reportlab) rendered at build time. The plan
   endpoint renders it too, so the size in the contents list is measured, not
   estimated.
6. **Cases are seeded.** `scripts/seed_cases.py` creates two demo case files from
   the existing recordings. The grouping is scaffolding, not a claim about a real
   investigation.
7. **Keyboard is partial.** `/` and `Esc` work; `j`/`k`, `space`, `v`, `c` are not
   wired yet.
8. **No density toggle.** The doc called for one; it is the first accessibility
   item still open.

---

## 14. "No match" returns an empty page: the corpus-scaled cosine floor

**Shipped.** In the previous pass this section was titled "Known limitation: no
match still returns a page", because four attempts at a threshold had failed. The
fifth worked, and the thing that changed was not the constant — it was making the
constant a function of corpus size.

**The symptom, for the record.** Cosine similarity has no natural zero. Every
sentence embedding sits in a narrow cone, so nonsense scores 0.27–0.35 against
ordinary English prose. Reciprocal Rank Fusion then hands every survivor a
positive score, so "nothing matched" was indistinguishable from "everything
matched weakly". `asdfghjkl` returned 25 rows in hybrid mode where keyword mode
correctly returned 0.

**The fix.** `search.py::_semantic_cutoff` now computes the cutoff in three
cases, and the reason it took is reported in diagnostics as `floor_reason`:

| Case | Cutoff | `floor_reason` |
|---|---|---|
| Lexical fallback embedder (`hashing-lexical-v2`) | `LEXICAL_MIN_SCORE` (0.05) | `lexical-fallback` |
| Corpus ≥ `SEMANTIC_FLOOR_REFERENCE_POOL` (1000) vectors | `SEMANTIC_MIN_SCORE` (0.40) | `absolute` |
| Smaller corpus | `SEMANTIC_MIN_SCORE × log10(n+1)/log10(reference+1)` | `scaled` |

Two details matter more than they look. First, `n` is how many vectors the store
**ranked**, not how many it returned — the score is a max over everything
scanned, and the top-N truncation happens afterwards, so passing the page size
would make the threshold depend on `limit`. Second, the backfill can never
manufacture a page out of an **empty** pool: an empty pool stops at step 1 of
`search()` and returns `exhibit_cap_backfilled: 0` and `total_candidates: 0`.
Every nonsense query reports both counters as 0. On a populated pool the backfill
fires normally — `police` reports `backfilled 5` of `total_candidates 25` — and is
bounded by the real candidate count, so it redistributes slots between exhibits
but cannot invent a row.

**Why a flat threshold failed and this does not.** Max-cosine-over-corpus is an
**extreme order statistic**: it grows with corpus size simply because a bigger
corpus gets more chances to contain a spurious near-miss. Measured over random
samples of the demo corpus, the mean max-cosine *of nonsense queries alone* is
0.158 at N=3, 0.195 at N=10, 0.241 at N=50, 0.283 at N=200 and 0.296 at N=500.
A single constant therefore cannot be right at two corpus sizes — which is why
the 0.38 attempt worked on the live corpus and broke the test fixture. Scaling by
the same log ratio turns the threshold from "how large is this cosine in absolute
terms" into "how extreme is this score for a corpus this size".

**Measured on the live corpus** (1865 segments, 6 recordings, 2 cases,
`all-MiniLM-L6-v2`, `limit=25`). Before = the old 0.05 default, after = 0.40.
Rows are hit counts, not scores.

| Query | Kind | Top cosine | semantic before → after | hybrid before → after | Rank-1 exhibit |
|---|---|---:|---|---|---|
| `police` | genuine | 0.5942 | 25 → 25 | 25 → 25 | unchanged |
| `smaller` | genuine | 0.5544 | 25 → 5 | 25 → 5 | unchanged |
| `much smaller than expected` | genuine | 0.5526 | 25 → 6 | 25 → 22 | unchanged |
| `small` | genuine | 0.5002 | 25 → 2 | 25 → 3 | unchanged |
| `woman hiding from an attacker` | genuine | 0.4600 | 25 → 2 | 25 → 3 | unchanged |
| `commencement address` | genuine | 0.4532 | 25 → 1 | 25 → 4 | unchanged |
| `trucks` | genuine | 0.4488 | 25 → 5 | 25 → 5 | unchanged |
| `truck` | genuine | 0.4403 | 25 → 3 | 25 → 3 | unchanged |
| `storage room capacity` | genuine | 0.3680 | 25 → 0 | 25 → 2 | unchanged (keyword) |
| `helicopter` | absent | 0.3584 | 25 → 0 | 25 → 0 | **correctly rejected** — see below |
| `presser` | filename | 0.3216 | 25 → 0 | 25 → 0 | **correctly rejected** — see below |
| `negative pressure room` | genuine | 0.2180 | 25 → 0 | 25 → 3 | unchanged (keyword) |
| `asdfghjkl` | nonsense | 0.3540 | 25 → **0** | 25 → **0** | — |
| `flurble wumpus` | nonsense | 0.3538 | 25 → **0** | 25 → **0** | — |
| `zzzqqq` | nonsense | 0.3035 | 25 → **0** | 25 → **0** | — |
| `qwertyuiop` | nonsense | 0.2990 | 25 → **0** | 25 → **0** | — |
| `xyzzy plugh` | nonsense | 0.2923 | 25 → **0** | 25 → **0** | — |
| `qqqzzz blurfl` | nonsense | 0.2676 | 25 → **0** | 25 → **0** | — |

Under the live embedder the noise ceiling is 0.3540 and the weakest genuine query
that must survive is `truck` at 0.4403. The floor sits 0.046 above the ceiling
and 0.040 below that floor-of-signal, which is the whole justification for 0.40
being the number rather than 0.36 or 0.45.

**The page-depth drop is the floor working, not damage.** A reviewer who sees
`commencement address` go from 25 rows to 4 will assume something broke. What
actually happened is that the 21 removed rows were the false-positive tail: their
semantic scores run 0.229–0.328, and they read as `Welcome to Highmark Stadium,
two powerhouses in section six`, `a template for work`, `I want each meeting.`,
`on the Moon Base website which launched, I believe,`. The genuine hits that
survive are the ones where the word or the meaning is actually present (`I got
her address.` at 0.4532, plus three keyword matches on "address"). Three rows
that answer the question beat twenty-five that bury them.

**`helicopter` and `presser` return nothing, and that is correct.**

Both are absent from the transcript. `helicopter` matches 0 segments; `presser`
appears only in the filename `eps_presser.flac`, which is not indexed. Before the
floor, `presser` matched `you were doing` at 0.3216 — noise that happened to sort
first. Nothing was lost, because nothing was there.

`helicopter` is the more interesting case, because the row it used to return
*looked* like an answer. It reached `In addition to flying two NASA science
payloads,` at **0.3584** — only 0.0044 above the worst noise query (`asdfghjkl`,
0.3540), i.e. inside the noise band. The reason is not that retrieval failed to
find the passage. It is that **a helicopter is not related to that passage**:

| pair | cosine |
|---|---:|
| `cos(helicopter, "flying")` — the concept it does share | **0.6365** |
| `cos(helicopter, the NASA passage)` | **0.3584** |
| `cos(aeroplane, the NASA passage)` | 0.4338 |
| `cos(rocket, the NASA passage)` | 0.4214 |
| `cos(aircraft, the NASA passage)` | 0.4936 |
| `cos(spacecraft, the NASA passage)` | 0.5632 |

The corpus passage is about a Blue Origin lunar mission. A helicopter is not a
science payload, not spacecraft, and not a lunar lander — so scoring it below the
floor is the floor doing its job. The concept the user *actually meant* still
resolves, which is the real test of whether retrieval broke. Live hybrid results
(`limit=5`; counts grow with the limit — `spacecraft` 8 and `lunar lander` 21 at
`limit=25`):

| query | rows | top hit |
|---|---:|---|
| `helicopter` | **0** | — |
| `aeroplane` | 1 | `In addition to flying two NASA science payloads,` |
| `aircraft` | 1 | `In addition to flying two NASA science payloads,` |
| `rocket` | 1 | `In addition to flying two NASA science payloads,` |
| `spacecraft` | 5 | `In addition to flying two NASA science payloads,` |
| `lunar lander` | 5 | `of the lunar surface.` |

Every near-synonym finds the passage. Only `helicopter` does not, and it should
not. The old 0.3584 row was a **spurious max** — the same artefact as `gun` →
`work with` — and it landed 0.0044 above `asdfghjkl` *precisely because it is
noise*. The margin arithmetic is still the evidence that the floor sits in a
narrow gap, but it now points the other way: no threshold can admit `helicopter`
without also admitting `asdfghjkl`, which is exactly why the floor stays at 0.40.

The residual cost is real but narrower than "genuine queries are lost": a user
who types a *loosely* associated term and expects the adjacent passage gets an
empty page instead of a ranked guess. That is the deliberate trade. In a
disclosure tool, an empty page is honest; a confident wrong row is not — a
reviewer handed `In addition to flying two NASA science payloads` for
`helicopter` would have to notice for themselves that it does not answer them,
and that is the disclosure-risk case. A cross-encoder reranker is what would let
the tool show the adjacent passage *and* say it is only loosely related; a lower
constant cannot, and would re-open the leak.

**What was tried, and why each was rejected.** Measured on the live corpus
(1865 segments, 6 recordings, 2 cases):

| Approach | Outcome |
|---|---|
| Raise `SEMANTIC_MIN_SCORE` to 0.38 (sits in the measured gap: noise 0.268–0.354, genuine 0.453–0.687) | Rejected all six nonsense queries and kept all nine genuine ones **on this corpus**. But it broke `test_semantic_real.py::test_real_semantic_finds_concept_without_a_literal_word` and starved `test_exhibit_cap.py::test_single_exhibit_query_is_not_starved_in_each_mode`. |
| …because the absolute value does not transfer between corpora | Max cosine over a **three-line** fixture for the genuinely related `small` → "much smaller" is **0.2882**, while max cosine over the **1865-segment live corpus** is **0.5002**. Both are maxima over a corpus, not like-for-like passage scores (that same passage against its best live neighbour is 0.2582), so they bound the effect rather than measure it passage-for-passage. Short passages carry less context, compressing their cosine toward zero, and any single constant rejects real hits somewhere. |
| Sweep 0.35 / 0.40 / 0.42 / 0.45 | 0.35 admits one nonsense hit; 0.40+ starts cutting genuine queries (`commencement address` 3 → 1 results). No value is both safe and effective across corpora. |
| Ratio test: keep scores ≥ some fraction of the top score | Fails in the wrong direction. `asdfghjkl` has second/top = **0.976** (all noise scores alike) while a genuine `truck` query has **0.263** (one clear best match). The noise looks *more* self-consistent than the signal. |

The third row is the honest ancestor of the shipped change: it is right that **no
flat value** is safe and effective across corpora. Its example of a genuine query
being cut — `commencement address` 3 → 1 — still describes the trade the floor
makes; what it does *not* describe is `helicopter`, which it is tempting to add to
the same list and which §14 above shows is a correct rejection rather than a
casualty. What changed is that the cutoff is no longer flat, so 0.40 is only
correct *together with* `SEMANTIC_FLOOR_REFERENCE_POOL`; the sweep was searching
for a universal constant, which does not exist. The fourth row was not tuned but
deleted: a relative floor is defined as a fraction of the top score, and when
every candidate is noise the top score is noise too, so the fraction of it is
still noise. Keeping it alongside the absolute floor would mean two thresholds
whose interaction has to be reasoned about, which is worse than one that is
calibrated.

**Residual caveat — this is calibrated, not guaranteed.** 0.40 was measured
against 1865 segments from six recordings with `all-MiniLM-L6-v2`. It is a
statement about that distribution, and the codebase cannot detect when the
statement stops being true. **The number is not the finding; the margins are**,
and they are narrow enough that they should be re-derived rather than inherited:

| margin | value | what it means |
|---|---:|---|
| floor above the noise ceiling (`asdfghjkl` 0.3540) | **0.0460** | how much headroom before a nonsense query leaks |
| floor below floor-of-signal (`truck` 0.4403) | **0.0403** | how much headroom before a genuine query is cut |
| `helicopter` (0.3584) above the noise ceiling (0.3540) | **0.0044** | why it must be *rejected* — inside the noise band, not a casualty |

Any re-tune or model change moves all three. Two things invalidate the constant:

- **Corpus growth.** Past the 1000-vector reference the floor stops scaling and
  becomes flat again, so noise *on a real deployment's corpus* can climb back
  toward it. Re-measure if a deployment's corpus differs by more than ~2×.
- **Model change.** `EMBEDDING_MODEL` is configurable. The lexical fallback
  already demonstrates that cosine scales are not comparable across embedders:
  measured on the 42-passage fixture, nonsense `qwertyuiop` scores **0.0342**
  under `hashing-lexical-v2` while genuine `small` scores **0.0233** — the
  fallback *ranks noise above signal*, which is why it keeps its own permissive
  floor instead of inheriting 0.40. Swapping models requires re-running the sweep
  above, not reusing this number.

A 0.046 / 0.040 split is narrow. It is not a comfortable middle of a wide gap —
it is a threshold wedged between two measured populations that are only 0.086
apart, and both edges have to be re-checked whenever the corpus or the embedder
moves. That is the cost of shipping a constant at all, and the reason §14 records
the measurements rather than just the verdict.

**Regression guard.** `test_semantic_floor.py` pins all three properties:
nonsense is rejected in every mode including one test that runs the real pipeline
with no patching, a small corpus does not starve, and the backfill cannot invent
a page from an empty pool. `test_semantic_real.py` and
`test_exhibit_cap.py::test_single_exhibit_query_is_not_starved_in_each_mode`
remain the guards against reaching for a flat constant again. **If raising the
floor breaks one of those, the floor is wrong — not the test.**

**What the UI does now.** The empty state and its suggestion chips are reachable
in every mode, not just keyword. A query that matches nothing gets an honest
empty page instead of a page of the least-bad fragments, and the weakest
surviving hits are no longer presented with the same confidence as the strongest.

---

## 15. One exhibit, one job at a time: a database invariant, not a check

**Shipped.** This records a correctness fix, because it changed a guarantee rather
than a screen, and the reasoning is the kind that gets undone by accident.

**The defect.** `POST /api/recordings/{id}/retry` read "is a job active?" and then
inserted one. Two callers both read *no* before either committed, so four
concurrent retries produced four jobs:

```
before:  500  500  500  200     4 jobs, 3 stuck at status="running"
after:   200  409  409  409     1 job, 0 stranded, still retryable
```

Three harms, in increasing order of seriousness. Four transcription+embedding
passes raced over the same rows. Three losers stayed `running` forever, because
`run_job()` returns early for a job it finds in `running`. And those three then
made every later retry answer **409** — `review_locked()` and the active-job
check both refuse while one is active — so the exhibit could never be
re-processed without hand-editing the database. For an evidence tool that is a
custody defect: an exhibit with a hole in its processing history.

**The invariant** is now a partial unique index, so it is a property of the
database rather than of any handler:

```sql
CREATE UNIQUE INDEX uq_job_active_recording
    ON jobs (recording_id) WHERE status IN ('queued', 'running')
```

*Partial* is the whole point. "At most one active job per recording" is the rule;
"at most one job per recording ever" would be a different and wrong one, because
finished jobs have to accumulate as audit history. It is declared once in
`Job.__table_args__` and installed for existing databases by
`db._ensure_active_job_uniqueness()`, which deduplicates first — the most recent
active job survives, older duplicates are marked `failed` with an explanatory
`error`, and **nothing is deleted**. A vanished job row would be a custody gap,
not a cleanup: it is the record of the duplicate run itself.

`IntegrityError` is translated to the same 409 the sequential path returns, from
one shared string (`errors.ACTIVE_JOB_MESSAGE`), so the two paths cannot drift
into two vocabularies.

**Every path that starts work takes that slot** — `/retry`, upload ingest, and
on-demand `/denoise`, which previously had its own check-then-act because it
enqueued no job. That gap had two distinct consequences, worth separating:

1. Concurrent denoise wrote the *same* derivative file mid-write, so ffmpeg read
   a truncated input and failed with an empty stderr — surfacing as
   `422 "level matching failed: "`, an error blaming audio levels for a race.
   Fixed by publishing atomically (`os.replace` from a private scratch file), so
   the canonical path is never observable in a partial state.
2. Even with the write atomic, all four callers completed and each appended
   `recording.denoised` to the audit trail for one artefact. `/provenance`
   collapses repeats into a count, so the chain of custody read **"denoised ×4"**
   for a derivative produced once — a reviewer reconstructing how many times it
   was rebuilt, and by whom, got a false answer. This was the more serious of the
   two, and it is why the fix is a claim rather than a quieter audit: four
   concurrent denoise calls now yield **1** event and three 409s.

**Recovery is explicit, never automatic.** A job whose process died is released
by `POST /api/jobs/{id}/reset` (or `/api/jobs/reset-stranded/{recording_id}`),
gated on `retry` and recorded against the actor with the stage, progress and
`started_at` observed at the time. It is deliberately *not* an age threshold:
`Job` has no heartbeat, and `transcribe()` on a two-hour cassette blocks for tens
of minutes without touching the database, so `started_at` age cannot distinguish
"dead" from "slow". A threshold would have to guess, and guessing wrong destroys
a live run's derivatives — worse than the bug. A human deciding, with the stage
and progress in front of them, is the accurate signal; the audit event is what
makes that decision reviewable later. Nothing is deleted, and the row survives as
`failed`/`reset`.

Two properties of a reset are worth stating for whoever runs the demo, because
both are easy to get backwards. **A reset starts no work.** It creates no job and
queues nothing — resetting a stranded denoise claim does not re-trigger a denoise
on that exhibit; it only makes the exhibit available again, so the expensive
rebuild happens if and when someone asks for it. The active-job row is a
mutual-exclusion token, not a scheduler entry, so releasing a lock is not the
same as enqueuing work. **A reset also cancels nothing.**
There is no cancellation mechanism, so a process that is genuinely alive keeps
running and can still write its own audit events afterwards; `failed`/`reset`
therefore records that an operator gave up on the run, not that the run stopped,
and the audited `stage`/`progress` snapshot is what tells the two apart later. If
a recording was stuck at `processing` it is moved to `failed`, with its existing
transcripts and segments left in place — but note what the *retry* then does,
which is not the same thing and was wrong in an earlier draft of this section. A
reset leaves the data alone; the retry it unblocks begins with
`_clear_previous_attempt()`, so it re-transcribes from scratch, and an attempt
that fails or finds ASR unavailable leaves the exhibit with no segments at all.
The honest statement is that reset restores **retryability**, not that it
preserves **the transcript**. §10 carries the same warning for whoever is
holding the remote.

The denoise claim is an ordinary `jobs` row (`kind="denoise"`, inserted `running`
— never `queued`, or `run_pending_jobs()` would execute it as a full ingest), so
it needs no denoise-specific recovery machinery: the same endpoint releases it.
The ingest pipeline's own denoise stage now publishes through the same
`_denoise_atomically()` helper, so both call sites have one safety argument
rather than two.

**Not hardened, on purpose.** `jobs.submit()` on the upload path has no 409
handler. Every path that creates a recording mints a fresh `recording_id`
server-side, so the insert provably cannot collide, and a handler for a state that
cannot occur is dead code implying a reachability that does not exist. `submit()`
still raises `ActiveJobConflict` rather than a bare `IntegrityError`, and
`test_job_seams.py` pins that at the function seam — the only place it can be
observed. **Do not reason about the enqueue path without the fresh-id fact.**

**Regression guards.** `test_retry_concurrency.py` widens the race with a
`threading.Barrier` so the window is deterministic rather than a coin flip
(`PROCESS_INLINE=0`, the worker deployment shape, since inline mode lets the
winner finish inside its own request and legitimately free the slot).
`test_job_migration.py` runs the migration against a database holding the exact
4-job corruption the defect produced. `test_stranded_job_recovery.py` covers
blocked → reset → 200, history preserved, permissions, that a reset creates no
job and rebuilds nothing, that a retry afterwards re-transcribes and can leave
the exhibit with no segments at all, and parameterises a 0 h /
0.5 h / 48 h job to pin that nothing is reset by age alone. `test_job_seams.py`
covers the denoise claim and the artefact. Each was confirmed to fail on the
unfixed code before being accepted — a concurrency test that passes without the
fix proves nothing.
