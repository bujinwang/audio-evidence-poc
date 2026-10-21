# Bug: "Ling" in Hybrid mode reports "No passages matched"

Reported by the client operator, 2026-10-20.

## Reproduction (verified in real Chrome, against the running app)

State: case `26-104772`, Find screen, Hybrid mode, exhibit chip **E-001** lit.

1. Click the **E-001** exhibit chip under the mode selector.
2. Type `Ling`, press Search.

Observed: `0 passages` and **"No passages matched. Try a different phrase, or widen the
scope to the archive."**

Expected: the eight passages that contain *Ling*, since `Ling` demonstrably matches.

## The search is correct; the *filter* is the surprise

`E-001` is `2022-09-07_911_call_phoenix.wav`. It does not contain the token. Measured
directly against the API, with the exact `recording_id` the browser sent:

| Query | Scope | Result |
|---|---|---|
| `Ling` | E-001 only (`rec_83026cac…`) | **0** |
| `Ling` | E-002 only (`rec_286854fc…`) | 8 |
| `Ling` | This case (E-001+E-002+E-003) | 8 |
| `Ling` | Whole archive | 8 |
| `employment` | E-001 only | 0 |
| `Edmonton` | E-001 only | 0 |

So the backend is right. `Ling` legitimately has zero hits inside the 911 call. The
defect is that the UI gives the operator **no usable signal that a narrow exhibit filter
is active and is the reason for the zero**, and then offers advice that is wrong.

## Why this is a defect and not user error

1. **The empty-state copy misattributes the cause.** It says *"Try a different phrase,
   or widen the scope to the archive."* Widening the scope does work (verified: 8
   passages after "Whole archive"). But "try a different phrase" cannot help inside a
   2.7-minute 911 call, and nothing in the sentence names the exhibit filter that is
   actually responsible. The operator's mental model — "the app can't find Ling" — is
   the one the copy leads them to.

2. **The exhibit chip looks identical to a mode button.** Both `.chip` and `.seg__i`
   render as pills in the same `.filters` row. Only a subtle `is-on` class distinguishes
   E-001 as a *filter* from Hybrid as a *mode*, and there is no text anywhere on the
   screen saying "filtered to E-001" next to the result count.

3. **The suggestion chips silently inherit the trapped filter.** On zero hits the
   screen offers `Try: <suggestions>`, but each suggestion calls `run(suggestion, mode)`
   with the same `exhibitFilter`, so a suggestion that does not occur in E-001 also
   comes back empty. (In this case the first suggestion happens to exist in E-001, so
   the operator escapes by luck, not by design.)

4. **The result count never states the scope it counted.** The line reads
   `0 passages · 1590 ms · hybrid · sentence-transformers`. It reports the engine but
   not the corpus, so `0 of 24 segments` and `0 of 1865 segments` are indistinguishable.

## Blast radius

Any exhibit filter plus any query absent from that exhibit produces the same false
"no match". `Ling` is one instance; `employment` and `Edmonton` reproduce it too. This
is the exact failure the product exists to avoid — an investigator concluding the audio
does not contain a name.

## Proposed fix (for the engineer to design)

- State the active scope in the result summary, e.g. `0 passages in E-001` vs
  `0 passages across 1865`.
- When the zero is caused by a filter, say so and make clearing it a one-click action
  inside the empty state ("Nothing in E-001. Search all 3 exhibits").
- Give exhibit filters a visual affordance distinct from mode buttons.
- Suggestion chips should not be offered, or should clear the filter, when they cannot
  possibly return a hit under the current filter.

## Non-issue confirmed

`"Ling"` also matches `filing`, `humbling`, `feeling` as a substring in keyword mode.
That is expected porter-tokenizer behaviour and is not part of this defect.

---

# Follow-up, same day: the *other* case, and a dead end in archive scope

The client came back with "it's still no passage", screenshot showing case **26-098431**
(public-order disclosure), Hybrid, *This case*, query `Ling`, no exhibit chip lit, and:

> No passages matched. Try a different phrase, or widen the scope to the archive.

## Why the first fix did not cover this

That fix addressed the case where an **exhibit filter** caused the zero. Here there is no
exhibit filter — the zero is genuine for the case, and the legacy copy was rendered
correctly by design. The remaining defect is different in kind: **the copy points at a
recovery route that only becomes discoverable after you take it.**

## Measured (headless Chrome, live app, case 26-098431)

| Scope | Query | Result |
|---|---|---|
| This case (179 segments) | `Ling` | **0** — correct, no exhibit here contains the token |
| Whole archive | `Ling` | **8** — all eight are `eps_presser.flac` in the *other* case |

So the advice is true. But nothing on the screen says the phrase exists elsewhere, or
that "the archive" contains another case at all. The word `Ling` is the client's own
principal, it is in the app, and the honest answer is "not in this case — it's in
26-104772" rather than "no passages matched".

## Second defect found while verifying: archive hits show no exhibit label

In archive scope the results table renders:

```
03:45.31 | —  | My name is Ling Ling Huang.
                 eps_presser.flac · 12/18/2024 · speaker unknown
```

The **Exhibit** column is `—` for every archive hit. Cause:
`Find.tsx` builds `labels` only from `caseFile.exhibits` (the current case), so
`labels[hit.recording_id]` is `undefined` for anything outside it, and the cell falls back
to `"—"`. Same reason `PlayerDock`'s `fallbackDuration` and `title` lose the label.

This is a cross-case identity bug in its own right: a passage from another case is
rendered as an anonymous exhibit in the one column that is supposed to say which file it
came from. It also means an operator who takes the advice and widens the scope still
cannot tell, from the Exhibit column, that they have left their case.

Note `E-001/E-002/E-003` collide across cases — case 26-104772's E-002 is the same file
that is unlabelled here. Any label shown in archive scope must not reuse a bare per-case
`E-00n`, or the two cases will contradict each other.
