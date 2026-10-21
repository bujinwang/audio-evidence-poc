import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Empty, Kbd, Notice } from "../components/Layout";
import { ReviewStamp, Stamp } from "../components/Stamp";
import PlayerDock from "../components/PlayerDock";
import { formatTimecode } from "../lib/format";
import type { CaseFile, SearchHit, SearchMode, SearchResponse } from "../types";

/**
 * Seed queries, keyed by what the case actually contains.
 *
 * A single hardcoded list was wrong the moment there was more than one case: the
 * default query for a homicide review means nothing in a public-order file, and
 * the screen opened on four unrelated fragments. Deriving the suggestions from
 * the exhibits' source types keeps the first result meaningful whichever case
 * the operator lands on. A deployment replaces this with saved searches.
 */
const SUGGESTIONS_BY_TYPE: Record<string, string[]> = {
  phone_call: ["woman hiding from an attacker", "someone asking for help"],
  sports: ["a football game on the radio", "the championship game"],
  speech: ["a mission to the moon", "a commencement address"],
  interview: ["who was in the meeting", "two people talking"],
  other: ["police funding", "a press conference"],
};

function suggestionsFor(caseFile: CaseFile): string[] {
  const seen: string[] = [];
  for (const exhibit of caseFile.exhibits) {
    for (const query of SUGGESTIONS_BY_TYPE[exhibit.source_type] ?? []) {
      if (!seen.includes(query)) seen.push(query);
    }
  }
  return seen.length ? seen.slice(0, 4) : SUGGESTIONS_BY_TYPE.other;
}

/**
 * How many passages the archive probe asks for.
 *
 * `SearchResponse.total` is `len(hits)`, and `search()` truncates that to
 * `limit`, so the count in "8 passages in case 26-104772 do" is capped by the
 * page size. This is the same 25 the screen's own searches use, which keeps the
 * probe's number on exactly the same footing as the `{total} passage(s)` in the
 * result summary — a probe with a smaller limit would not be cheaper in a way
 * the operator could not notice, it would just report a different (smaller)
 * number than the search they are about to run.
 */
const PROBE_LIMIT = 25;

/** Everything the screen needs to name one recording, from whatever case it is. */
interface RecordingRef {
  label: string;
  caseNumber: string;
  filename: string;
  duration: number | null;
  foreign: boolean;
  /** Whether a denoised derivative exists, so the dock only offers variants that resolve. */
  hasDenoised: boolean;
}

/**
 * The answer to "is this phrase in the archive but not in this case?".
 *
 * `hits` is how many passages the probe found outside the open case, `cases` is
 * which cases they are in (case number → how many), and `query` is the text that
 * was probed — kept so the copy can be proved to describe the current query
 * rather than an older one whose request happened to land first.
 */
interface ArchiveProbe {
  query: string;
  hits: number;
  cases: { number: string; count: number }[];
}

/**
 * The Exhibit column of one result row.
 *
 * In case scope this is the familiar `E-002`. In archive scope a hit can come
 * from a case that is not open, and a bare per-case label is genuinely
 * ambiguous there — case 26-104772's E-002 and 26-098431's E-002 are different
 * files — so the case number is stamped on a line above the label. The column
 * is 62px wide at this density and cannot hold `26-104772 E-002` on one line
 * without truncating one of them.
 *
 * `qualify` is driven by the *result* scope, not by whether this particular
 * recording is foreign. Meaured in Chrome: keying it on foreignness alone left
 * 26-104772's own archive hits rendering a bare `E-002` that looked identical to
 * their case-scope rows, so the column still could not tell the operator they had
 * left their case — which is half of what the follow-up asked for. In archive
 * scope every row is cross-case, including the ones that happen to be home.
 */
function ExhibitLabel({ entry, qualify }: { entry: RecordingRef | undefined; qualify: boolean }) {
  if (!entry) return <>—</>;
  if (!qualify) return <>{entry.label}</>;
  return (
    <>
      <span className="exhibitcell__case">{entry.caseNumber}</span>
      <span className="exhibitcell__label">{entry.label}</span>
    </>
  );
}

export default function Find({
  caseFile,
  cases,
  onOpenReview,
}: {
  caseFile: CaseFile;
  /**
   * Every case in the archive. Only `number` and `exhibits` are read, for two
   * things this screen could not do from `caseFile` alone: naming a hit that
   * came back from another case in archive scope, and — on a case-scope zero —
   * telling the operator which case the phrase is actually in.
   *
   * Optional so the screen still renders without it (an older caller, a test);
   * the cross-case features degrade to the previous behaviour rather than
   * throwing.
   */
  cases?: CaseFile[];
  onOpenReview: (recordingId: string, segmentId?: string) => void;
}) {
  const suggestions = useMemo(() => suggestionsFor(caseFile), [caseFile]);
  const [query, setQuery] = useState(suggestions[0]);
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [scope, setScope] = useState<"case" | "all">("case");
  const [exhibitFilter, setExhibitFilter] = useState<string>("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  // The scope the *stored* response was actually counted over — not the scope
  // the control row is showing right now. Kept beside `results` rather than
  // derived from `scope`/`exhibitFilter` at render time because a filter change
  // is followed by a ~1.5 s request during which the previous counts are still
  // on screen; reading the live state there would label E-001's total "across
  // 1865 segments", which is the exact class of mismatch this state exists to
  // remove. `null` until the first response lands, so no count is ever
  // summarised with a scope it was not measured in.
  const [resultScope, setResultScope] = useState<{
    scope: "case" | "all";
    exhibit: string;
  } | null>(null);
  const [selected, setSelected] = useState<SearchHit | null>(null);
  const [jumpToken, setJumpToken] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * Result of the archive probe for a case-scope zero, or `null`.
   *
   * Held separately from `results` on purpose: the probe is a *question about
   * the zero*, not a search the operator asked for, and it must never replace
   * the table, the summary line, or `resultScope`. If it wrote into those, the
   * screen would silently show archive results under a "This case" chip — the
   * precise category of bug this whole area exists to remove.
   */
  const [archiveProbe, setArchiveProbe] = useState<ArchiveProbe | null>(null);
  /**
   * The most recently *issued* search, as a monotonic number.
   *
   * `api.search` has no ordering guarantee, and the exhibits differ in size by
   * ~39x (E-003 is 20 segments, E-002 is 773), so clicking two chips in quick
   * succession reliably resolves them out of order. Without this, whichever
   * response happened to land last wrote itself into `results`/`resultScope`
   * regardless of which one the operator had actually asked for *last* — the
   * screen then showed the chip for E-002 next to "0 passages in E-001", i.e. it
   * declared the one file that contains the name to be empty.
   *
   * A `useRef`, not state: the token is read and compared inside an async
   * continuation, where a state variable would be the value from the render the
   * closure was created in — exactly the staleness being fixed. Nothing renders
   * from it, so it must not trigger renders either.
   */
  const requestToken = useRef(0);
  const input = useRef<HTMLInputElement | null>(null);

  const labels = useMemo(() => {
    const map: Record<string, string> = {};
    caseFile.exhibits.forEach((exhibit, index) => {
      map[exhibit.id] = `E-${String(index + 1).padStart(3, "0")}`;
    });
    return map;
  }, [caseFile.exhibits]);

  /**
   * Every exhibit in the archive, keyed by recording id.
   *
   * `labels` above is built from `caseFile.exhibits` only, which is enough while
   * every hit comes from the open case — and is exactly why archive scope used
   * to render `—` in the Exhibit column for every row: `labels[hit.recording_id]`
   * is `undefined` for a recording in another case, and the cell fell back to a
   * dash. A passage from a different case was therefore anonymous in the one
   * column that is supposed to say which file it came from.
   *
   * Built from `cases` (already loaded by `App` for the rail and the queue), so
   * this costs no request. `foreign` records whether the recording belongs to
   * the open case, which is what decides between the bare `E-002` label and the
   * case-qualified one: a per-case label is only unambiguous inside its own case.
   */
  const recordings = useMemo(() => {
    const map: Record<string, RecordingRef> = {};
    const known = cases && cases.length ? cases : [caseFile];
    for (const file of known) {
      file.exhibits.forEach((exhibit, index) => {
        map[exhibit.id] = {
          label: `E-${String(index + 1).padStart(3, "0")}`,
          caseNumber: file.number,
          filename: exhibit.filename,
          duration: exhibit.duration_s,
          foreign: file.id !== caseFile.id,
          hasDenoised: exhibit.has_denoised,
        };
      });
    }
    // The open case is authoritative for its own exhibits, in case `cases` is
    // stale relative to `caseFile` (a re-upload lands in `caseFile` first).
    caseFile.exhibits.forEach((exhibit, index) => {
      map[exhibit.id] = {
        label: `E-${String(index + 1).padStart(3, "0")}`,
        caseNumber: caseFile.number,
        filename: exhibit.filename,
        duration: exhibit.duration_s,
        foreign: false,
        hasDenoised: exhibit.has_denoised,
      };
    });
    return map;
  }, [cases, caseFile]);

  /**
   * Is this phrase in the archive but not in the open case?
   *
   * Called from `run` and nowhere else, and only after a search has already come
   * back with **zero** hits. That gate is the whole cost argument: a successful
   * search never probes, so the common path issues exactly one request, and the
   * second request exists only for the case the operator is stuck in. Probing
   * unconditionally would double the load and, worse, would put a number on
   * screen for a query the operator did not ask about.
   *
   * The probe is deliberately *not* `mode`-parametric beyond what the zero told
   * us: it reuses the mode the operator chose, because a phrase that reaches the
   * archive only by semantic similarity should be reported as reachable, and
   * re-running keyword when they asked for hybrid would under-report.
   *
   * Archive-side hits are then attributed to cases from `recordings`, which is
   * built from `cases` locally — no second request for the attribution, and no
   * new backend field.
   */
  const probeArchive = useCallback(
    async (text: string, nextMode: SearchMode, token: number): Promise<void> => {
      // Nothing to compare against if the archive is just this case, and nothing
      // to gain from a probe the screen could not act on.
      if (!cases || cases.length < 2) return;
      try {
        // No `recording_id`: this is the whole archive, which is exactly the
        // corpus "Whole archive" will search when the operator takes the escape.
        const response = await api.search({ q: text, mode: nextMode, limit: PROBE_LIMIT });
        // The probe outlives `run`'s own guard: it is awaited *after* the point
        // where `run` already proved it was current, so it needs the same check
        // again on the way out. Without it, a slow probe belonging to a
        // superseded zero would publish an elsewhere-count for a query the
        // screen is no longer showing — a number attached to the wrong question,
        // which is the failure this whole area keeps producing.
        if (token !== requestToken.current) return;
        // Keep only hits from outside the open case. A hit inside it cannot
        // happen — the case search just returned zero for the same query — but
        // filtering on the recording's home case rather than trusting that makes
        // the number mean "passages elsewhere" by construction, and lets the
        // copy say "in another case" without hedging.
        const elsewhere = response.hits.filter((hit) => {
          const entry = recordings[hit.recording_id];
          return entry?.foreign ?? false;
        });
        if (elsewhere.length === 0) {
          setArchiveProbe({ query: text, hits: 0, cases: [] });
          return;
        }
        const perCase = new Map<string, number>();
        for (const hit of elsewhere) {
          const number = recordings[hit.recording_id]?.caseNumber ?? "";
          perCase.set(number, (perCase.get(number) ?? 0) + 1);
        }
        setArchiveProbe({
          query: text,
          // `total` is the length of the returned page, so trust the filtered
          // array instead of the reported total: the probe's own page may be
          // truncated by `limit`, but the hits it *did* return are all real.
          hits: elsewhere.length,
          cases: [...perCase.entries()]
            .map(([number, count]) => ({ number, count }))
            .sort((left, right) => right.count - left.count),
        });
      } catch {
        // A probe is an optional enhancement of an empty state that is already
        // correct without it. A failure here must not turn a working zero into
        // an error banner, so it degrades to the legacy copy — but a *stale*
        // probe's failure must not even do that, or it would blank the copy of a
        // newer query that is still settling.
        if (token === requestToken.current) setArchiveProbe(null);
      }
    },
    [cases, recordings],
  );

  /**
   * Issue the search described by the current filter state.
   *
   * `run` reads `scope` and `exhibitFilter` from the render it was created in,
   * so it is only ever called from an event handler of that same render or from
   * the effect below — never from a handler that has just queued a state change.
   * That is the whole point of the effect: a filter change is expressed once, as
   * state, and the request follows from the state rather than from a handler
   * passing arguments by hand.
   */
  const run = useCallback(
    async (text: string, nextMode: SearchMode): Promise<SearchResponse | null> => {
      if (!text.trim()) return null;
      // Claim the newest token *before* the await. Anything that settles after
      // this point but carries an older number is a superseded request and must
      // leave the screen entirely alone — see the guard below.
      const token = ++requestToken.current;
      setLoading(true);
      setError(null);
      setArchiveProbe(null);
      try {
        const response = await api.search({
          q: text,
          mode: nextMode,
          // An exhibit narrows the search to one recording. An exhibit belongs
          // to exactly one case, so it is a filter *within* the case scope, and
          // the exhibit branch has to be checked before the scope branch — the
          // other way round it is unreachable while scope is "case".
          recording_id: exhibitFilter
            ? [exhibitFilter]
            : scope === "case"
              ? caseFile.exhibits.map((exhibit) => exhibit.id)
              : undefined,
          limit: 25,
        });

        // The ordering guard. Everything below this line mutates the screen, so
        // a superseded response must return *here*: discarding only `setResults`
        // would still let an older request move `setResultScope` or, worse,
        // attach its own probe to a newer query. Returning the response anyway
        // keeps the function's contract (the bootstrap path awaits it) while
        // making it a no-op for the UI.
        //
        // `setLoading` is deliberately untouched on this path: the newer request
        // that superseded this one owns the loading state now, and clearing it
        // here would hide a spinner for a search still in flight. The `finally`
        // below is therefore not the place to reset it either.
        if (token !== requestToken.current) return response;

        setResults(response);
        // Record the scope *this* response was measured over, in the same
        // commit as the response itself, so the summary line and the table can
        // never describe different corpora.
        setResultScope({ scope, exhibit: exhibitFilter });
        setSelected(response.hits[0] ?? null);
        setJumpToken((next) => next + 1);

        // A zero in case scope may not be a zero in the archive. Ask, but only
        // when the cheap test says it is worth asking — see `probeArchive`.
        //
        // `!exhibitFilter && scope === "case"` is load-bearing, not defensive.
        // The probe answers "is this phrase somewhere *wider* than what I
        // searched?", and that question only exists when what was searched was
        // the whole case. Measured in Chrome: without this gate, E-001 + `Ling`
        // in 26-104772 issued a second unmetered archive request whose answer the
        // filtered branch never displays — the filtered empty state already
        // carries a one-click clear, so the probe would be pure cost.
        //
        // `await`ed rather than fired and forgotten so `loading` stays true
        // until the copy is final. Otherwise the legacy "No passages matched"
        // sentence paints first and is replaced a beat later by the real
        // answer, which reads as the app changing its mind.
        if (response.hits.length === 0 && !exhibitFilter && scope === "case") {
          await probeArchive(text, nextMode, token);
        }
        return response;
      } catch (err) {
        // A failure from a superseded request is not the operator's problem and
        // must not replace a newer, successful result with an error banner.
        if (token === requestToken.current) {
          setError(err instanceof Error ? err.message : "Search failed");
        }
        return null;
      } finally {
        // Only the current request may end the loading state. A stale response
        // resolving after a newer one has started would otherwise drop the
        // spinner while the newer request is still running, leaving the screen
        // interactive over results that are about to change again.
        if (token === requestToken.current) setLoading(false);
      }
    },
    // `probeArchive` is declared above and is a `useCallback` over `cases` and
    // `recordings`, both of which only change when the open case or the archive
    // listing does — so `run` does not change identity per search, and the
    // bootstrap / `filterKey` effects cannot be retriggered by it.
    [caseFile.exhibits, scope, exhibitFilter, probeArchive],
  );

  /**
   * The search came back empty.
   *
   * This is the single place that decides "the operator needs the suggestion
   * chips back". Every search funnels through here — the seeded query on load, a
   * submitted query, a filter change — so a zero-hit result always offers a way
   * out. Previously the chips rendered only while `results === null`, and the
   * only code that set it back to `null` was the bootstrap path, so after any
   * explicit search that found nothing the empty-state copy told the operator to
   * try something else while the chips that would let them do that were hidden.
   *
   * Derived rather than stored: setting `results` to null here would discard the
   * response the empty-state copy and the result count are read from, and would
   * mean clearing an object only to re-derive "there are no hits" from nothing.
   */
  const noHits = results !== null && results.hits.length === 0;

  // Open the screen with results rather than a blank page.
  //
  // The seeded query may resolve to nothing in this case, in which case the
  // operator should see the suggestion chips rather than an empty table that
  // looks like a broken search. That decision is not made here: it is derived
  // above from `noHits`, so every search takes the same path. (It used to be
  // duplicated here as a `setResults(null)` that only the bootstrap could
  // trigger, which is why the chips came back on load and never came back
  // again.)
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void run(query, mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-run the search whenever a filter changes.
  //
  // The filter row used to be wired handler-by-handler: each chip mutated state
  // and some of them also tried to re-query — with the *previous* value, because
  // the callback it called was captured before the change. The result was chips
  // that lit up while the table behind them stayed put, and an "All" chip that
  // re-issued the single-exhibit query. Making the filter state the single
  // source of truth removes the class of bug: handlers only set state, and this
  // effect turns state into exactly one request.
  //
  // The comparison against the previous key (rather than a "skip first render"
  // flag) keeps the bootstrap search the only request on mount, even when React
  // runs effects twice in development.
  const filterKey = `${mode}|${scope}|${exhibitFilter}`;
  const previousFilterKey = useRef(filterKey);
  useEffect(() => {
    if (previousFilterKey.current === filterKey) return;
    previousFilterKey.current = filterKey;
    void run(query, mode);
    // `query` is deliberately not a dependency: it is the search box's draft
    // text, and re-querying on every keystroke would be wrong. When a filter
    // moves, the current draft is the text that gets searched.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey]);

  useEffect(() => {
    input.current?.focus();
  }, []);

  /**
   * Where the counts currently on screen were counted — in words.
   *
   * Read from `resultScope`, the scope recorded next to `results`, and never
   * from the live `scope`/`exhibitFilter`. The two differ for the ~1.5 s a
   * request is in flight, and labelling a response that was measured over one
   * 2.7-minute exhibit "across 1865 segments" would be a worse lie than the
   * unnamed scope this replaces. `null` until the first response lands.
   *
   * The segment denominator comes from `caseFile`, which the screen already
   * holds: `totals.segments` for the case, `exhibit.segment_count` for one
   * exhibit. No extra request, no new backend field.
   */
  const countedIn = useMemo(() => {
    if (!resultScope) return null;
    if (resultScope.exhibit) {
      const exhibit = caseFile.exhibits.find((item) => item.id === resultScope.exhibit);
      return {
        where: `in ${labels[resultScope.exhibit] ?? "one exhibit"}`,
        segments: exhibit?.segment_count ?? null,
      };
    }
    if (resultScope.scope === "all") {
      // The archive has no segment total on this screen, and inventing one
      // would be the same defect in a new place. Say the scope, omit the count.
      return { where: "across the whole archive", segments: null };
    }
    return { where: "across this case", segments: caseFile.totals.segments };
  }, [caseFile, labels, resultScope]);

  /**
   * The exhibit filter that is blocking recovery on the current screen, if any.
   *
   * Two ids can be responsible and both must be caught, or a way out is lost:
   *
   * - `resultScope.exhibit` — the filter that actually produced the response on
   *   screen. This is what the empty-state copy must describe, because it is the
   *   search that came back empty.
   * - `exhibitFilter` — the filter currently set on the controls. It differs
   *   from the recorded one when a re-query has not landed: a request that
   *   failed, or a query that was blank and returned before `run` issued
   *   anything. In that window the old response is still displayed, so a
   *   suggestion chip would inherit the new filter and silently return zero.
   *
   * The recorded id wins when both are set, because the explanation has to
   * match the counts above it. The live id is the fallback that keeps the escape
   * hatch on screen in the failed-request case rather than leaving the operator
   * with "try a different phrase" and no chips to try.
   */
  const blockedBy = resultScope?.exhibit || exhibitFilter;
  const blockedLabel = blockedBy ? (labels[blockedBy] ?? "this exhibit") : "";
  // How small the corpus the zero was measured in actually is. This is the fact
  // that turns "the app cannot find Ling" into "I searched 24 segments out of
  // 1865", and it is already on `caseFile` — no extra request, no new field.
  const blockedSegments = blockedBy
    ? (caseFile.exhibits.find((exhibit) => exhibit.id === blockedBy)?.segment_count ?? null)
    : null;

  // Escape hatch shared by the empty state and the untrapped chips: only state
  // is set here, and the `filterKey` effect above turns it into the request.
  // Calling `run()` directly would search the filter this render was created
  // with — the trap being escaped.
  const clearExhibitFilter = useCallback(() => setExhibitFilter(""), []);

  /**
   * Widen to the archive and re-run.
   *
   * The same shape as `clearExhibitFilter`, for the same reason: two state
   * setters and no `run()` call. `run` closes over `scope` and `exhibitFilter`
   * from the render it was created in, so calling it here would issue the
   * *case* search again under an "archive" chip — the failure mode the
   * `filterKey` effect exists to prevent. Setting both keys changes `filterKey`,
   * and the effect issues exactly one request.
   *
   * The exhibit filter is cleared too: it names a recording inside the open
   * case, so keeping it would narrow the widened search back to the exhibit that
   * has already been proved empty. That is the round-1 trap, one level up.
   */
  const widenToArchive = useCallback(() => {
    setScope("all");
    setExhibitFilter("");
  }, []);

  /**
   * The probe result, but only while it still describes what is on screen.
   *
   * Three conditions, each of which has been a real way for this copy to lie:
   * the stored response must still be the empty one the probe was run for
   * (`noHits`), it must not itself have been an archive search (`resultScope`
   * unfiltered case scope — there is nothing wider to point at), and it must be
   * the same query text. The last one matters because the probe is awaited
   * inside `run`: a quick second search could otherwise land its own zero first
   * and be labelled with the previous query's findings.
   */
  const probeApplies =
    noHits &&
    !resultScope?.exhibit &&
    resultScope?.scope === "case" &&
    archiveProbe !== null &&
    archiveProbe.query === results.query;

  // Named after the scope the *empty response* was measured in, so the label
  // matches the count above it. `scope` and `resultScope.scope` agree whenever
  // this is rendered — changing scope clears the exhibit filter, and the
  // resulting request holds `loading` true in the meantime — but reading it from
  // the recorded scope keeps the promise true by construction rather than by
  // that argument.
  const escapeLabel =
    (resultScope?.scope ?? scope) === "all"
      ? "Search the whole archive"
      : caseFile.exhibits.length > 1
        ? `Search all ${caseFile.exhibits.length} exhibits`
        : "Search the whole case";

  // The selected passage's recording, resolved across cases for the same reason
  // the table cell is: in archive scope the selection can be a file from another
  // case, and `caseFile.exhibits` has nothing to say about it. `fallbackDuration`
  // in particular used to be `undefined` — and therefore `null` — for every
  // archive hit, so the player fell back to trusting the audio element's own
  // metadata that was never loaded.
  const selectedExhibit = selected ? recordings[selected.recording_id] : undefined;

  return (
    <div className="view" style={{ paddingBottom: 64 }}>
      <div className="pagehead">
        <div>
          <h1>Find</h1>
          <p>
            One query across the case. Every hit is a passage you can play, not a document you have
            to re-read.
          </p>
        </div>
        <div className="pagehead__right">
          <button
            className={`chip ${scope === "case" ? "is-on" : ""}`}
            aria-pressed={scope === "case"}
            // Choosing a scope resets the narrower exhibit filter. Leaving it
            // set would mean "Whole archive" kept querying a single exhibit —
            // the chip lit while the results ignored the scope entirely. Scope
            // is the outer filter, so it clears the inner one, visibly: the
            // All chip lights up at the same time.
            onClick={() => {
              setScope("case");
              setExhibitFilter("");
            }}
          >
            This case
          </button>
          <button
            className={`chip ${scope === "all" ? "is-on" : ""}`}
            aria-pressed={scope === "all"}
            onClick={() => {
              setScope("all");
              setExhibitFilter("");
            }}
          >
            Whole archive
          </button>
        </div>
      </div>

      <form
        className="searchbar"
        onSubmit={(event) => {
          event.preventDefault();
          void run(query, mode);
        }}
      >
        <span className="searchbar__icon">
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.6">
            <circle cx="7" cy="7" r="3.6" />
            <path d="M10 10l3.5 3.5" />
          </svg>
        </span>
        <input
          ref={input}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Describe what you are looking for…"
          aria-label="Search"
        />
        <Kbd>/</Kbd>
        <button className="btn btn--primary btn--sm" type="submit" disabled={loading}>
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      <div className="filters">
        {/*
          The mode segments and the exhibit chips used to be two adjacent sets of
          pills in this one row, distinguishable only by which one carried
          `is-on`. An operator who had clicked E-001 had no way to see that the
          pill reading "E-001" narrowed the corpus while "Hybrid" only chose an
          algorithm — the two controls looked like the same kind of thing, and
          the zero-hit state blamed the phrase. Each group now carries its own
          label, and the exhibit chips are shaped and coloured differently from
          the mode segments (square, mono, outlined rather than filled) so the
          distinction survives without reading the labels.
        */}
        <div className="filtergroup">
          <span className="filtergroup__k">Mode</span>
          <div className="seg" role="tablist" aria-label="Search mode">
            {(["hybrid", "keyword", "semantic"] as SearchMode[]).map((value) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={mode === value}
                className={`seg__i ${mode === value ? "is-on" : ""}`}
                // The re-query follows from the state change, in the effect above.
                // Calling `run()` here would search the mode this render was
                // created with, not the one just selected.
                onClick={() => setMode(value)}
              >
                {value === "hybrid" ? "Hybrid" : value === "keyword" ? "Keyword" : "Semantic"}
              </button>
            ))}
          </div>
        </div>

        {/*
          Exhibit chips narrow the search to a single exhibit *within the current
          scope*. An exhibit belongs to exactly one case, so this is a strictly
          narrower filter than "This case" and composes with either scope: with
          "Whole archive" selected, restricting to one exhibit is still a
          meaningful (and identical) query, not a contradiction. They used to
          also flip the scope to "all", which made the result depend on which of
          two adjacent chips had been clicked first — the scope chip then read
          "Whole archive" after the operator clicked an exhibit inside a case.
        */}
        <div className="filtergroup" role="group" aria-label="Exhibit filter">
          <span className="filtergroup__k">Exhibit filter</span>
          <button
            className={`chip chip--filter ${exhibitFilter === "" ? "is-on" : ""}`}
            aria-pressed={exhibitFilter === ""}
            onClick={() => setExhibitFilter("")}
          >
            All
          </button>
          {/*
            Every exhibit is listed. This was `caseFile.exhibits.slice(0, 4)`,
            which silently hid the rest of a case's exhibits with nothing on
            screen saying more existed — an exhibit simply could not be filtered
            to. The row already wraps, so length is not a layout constraint.
          */}
          {caseFile.exhibits.map((exhibit, index) => (
            <button
              key={exhibit.id}
              className={`chip chip--filter ${exhibitFilter === exhibit.id ? "is-on" : ""}`}
              aria-pressed={exhibitFilter === exhibit.id}
              onClick={() => setExhibitFilter(exhibit.id)}
            >
              {labels[exhibit.id] ?? `E-${index + 1}`}
            </button>
          ))}
          {/* One-click exit from the filter, next to the control that set it —
              the same action the empty state offers, reachable before it is
              needed. Only rendered when it would do something. */}
          {exhibitFilter !== "" && (
            <button
              type="button"
              className="chip chip--filter chip--clear"
              onClick={clearExhibitFilter}
              title="Clear the exhibit filter and search the whole scope"
            >
              Clear
            </button>
          )}
        </div>

        {/*
          The count now states the corpus it was counted over. It used to read
          `0 passages · 1590 ms · hybrid · sentence-transformers`: the engine,
          never the scope, so "0 of 24 segments" and "0 of 1865 segments" were
          the same line and the operator could not tell a filtered zero from a
          genuine one. `countedIn` is derived from the scope recorded with this
          response, not from the live controls, so it stays truthful while a
          re-query is in flight.
        */}
        {results && (
          <span className="hint resultsummary" style={{ marginLeft: "auto" }}>
            {results.total} passage{results.total === 1 ? "" : "s"}
            {countedIn && (
              <>
                {" "}
                <span
                  className={`resultsummary__scope ${
                    resultScope?.exhibit ? "resultsummary__scope--filtered" : ""
                  }`}
                >
                  {countedIn.where}
                </span>
                {countedIn.segments !== null && (
                  <span className="resultsummary__segments">
                    {" "}
                    · {countedIn.segments} segment{countedIn.segments === 1 ? "" : "s"} in scope
                  </span>
                )}
              </>
            )}{" "}
            · {results.took_ms} ms · {results.mode}
            {results.diagnostics.embedder ? ` · ${results.diagnostics.embedder}` : ""}
            {results.diagnostics.semantic === false ? " (lexical fallback)" : ""}
            <span style={{ marginLeft: 10 }}>
              <Stamp tone="plain">a match is not proof — listen first</Stamp>
            </span>
          </span>
        )}
      </div>

      {error && (
        <div className="results" style={{ paddingTop: 12 }}>
          <Notice tone="error">{error}</Notice>
        </div>
      )}

      {/*
        Shown before any search has run, and again whenever a search found
        nothing (`noHits`) — that is the recovery affordance the empty-state copy
        below points at, so it must be reachable in both cases, not just on load.
        Held back while a request is in flight so the chips do not flash between
        the empty state and its replacement. (A previous defect was these chips
        vanishing after the first zero-hit search; `noHits` is derived from
        `results`, and this condition is what makes them come back. Do not
        narrow it to `results === null`.)

        With an exhibit filter in force the row is omitted entirely. Each chip
        calls `run(suggestion, mode)` and would silently inherit the same filter,
        so a suggestion absent from that exhibit comes back empty and the
        operator is back where they started — that is how the reported trap
        reproduced, escaped only because the first suggestion happened to exist
        in E-001. The guaranteed escape is offered in the empty state instead,
        where the explanation for the zero is.

        Two conditions, not one: `blockedBy` covers the filter that produced the
        empty result on screen *and* a filter that is set but has not been
        queried yet (a failed request); a chip under either one can return zero
        for a reason the operator cannot see.
      */}
      {(!results || noHits) && !loading && !blockedBy && (
        <div className="results" style={{ paddingTop: 18 }}>
          <div className="suggestions" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <span className="hint">Try:</span>
            {suggestions.map((suggestion) => (
              <button
                key={suggestion}
                className="chip"
                onClick={() => {
                  setQuery(suggestion);
                  void run(suggestion, mode);
                }}
              >
                {suggestion}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="results">
        {loading && <Empty>Searching…</Empty>}
        {/*
          Same `noHits` the suggestion chips use, so the explanation and the
          recovery affordance can never disagree about whether the search came
          back empty. Kept alongside the chips rather than replaced by them: this
          line is the only place that says why the table is empty.

          Three branches, because the three zeros need different advice:

          1. **An exhibit filter caused it.** "Try a different phrase" is useless
             inside one 2.7-minute file: the phrase is not there and no rephrasing
             will put it there, so this branch names the exhibit, gives its size,
             and carries the one-click clear.
          2. **Case scope, and the phrase is in the archive.** The zero is real
             for the case, so the legacy sentence would be true — but it points at
             a recovery route with no reason to take it, and for the client's own
             principal's name that reads as "the app has nothing". If the probe
             found passages in another case, say how many and which case, and put
             the widening behind one button.
          3. **Nothing anywhere.** The legacy sentence, verbatim, unchanged. The
             `zzzqqq` path and its suggestion chips are untouched by this round.
        */}
        {!loading && noHits && (
          <Empty>
            {resultScope?.exhibit ? (
              <div className="zeroscope">
                <p>
                  Nothing in {blockedLabel} matches “{results?.query}”. {blockedLabel}
                  {blockedSegments !== null
                    ? ` holds ${blockedSegments} transcript segment${blockedSegments === 1 ? "" : "s"}`
                    : " is a single exhibit"}
                  , so a phrase that is absent from it always comes back empty however it is
                  worded. It may still be in another exhibit.
                </p>
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  onClick={clearExhibitFilter}
                  title={`Remove the ${blockedLabel} filter and re-run this query`}
                >
                  {escapeLabel}
                </button>
              </div>
            ) : probeApplies && archiveProbe && archiveProbe.hits > 0 ? (
              <div className="zeroscope">
                <p>
                  Nothing in this case matches “{results.query}”.{" "}
                  <strong>
                    {archiveProbe.hits} passage{archiveProbe.hits === 1 ? "" : "s"}
                  </strong>{" "}
                  {archiveProbe.hits === 1 ? "does" : "do"} —{" "}
                  {archiveProbe.cases.length === 1
                    ? `all of them in case ${archiveProbe.cases[0].number}`
                    : `in ${archiveProbe.cases
                        .map((entry) => `case ${entry.number}`)
                        .join(", ")}`}
                  .
                </p>
                <button
                  type="button"
                  className="btn btn--primary btn--sm"
                  onClick={widenToArchive}
                  title="Search the whole archive with this query"
                >
                  Search the whole archive
                </button>
              </div>
            ) : (
              <div>
                No passages matched. Try a different phrase, or widen the scope to the archive.
                {/* The response was not filtered, so this zero is genuine for
                    the scope it was counted in — the sentence above is correct
                    and is kept verbatim. But a filter the operator has set since
                    has not been applied yet, so it is still offered as a way
                    out rather than left to trap the next search. */}
                {blockedBy && (
                  <div style={{ marginTop: 10 }}>
                    <button
                      type="button"
                      className="btn btn--sm"
                      onClick={clearExhibitFilter}
                      title={`Remove the ${blockedLabel} filter and re-run this query`}
                    >
                      {escapeLabel}
                    </button>
                  </div>
                )}
              </div>
            )}
          </Empty>
        )}
        {results && results.hits.length > 0 && (
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ width: 34 }} />
                <th style={{ width: 66 }}>Time</th>
                <th style={{ width: 62 }}>Exhibit</th>
                <th>Passage</th>
                <th style={{ width: 80 }}>Signals</th>
                <th style={{ width: 96 }}>Status</th>
                <th className="num" style={{ width: 52 }}>
                  Score
                </th>
              </tr>
            </thead>
            <tbody>
              {results.hits.map((hit, index) => (
                <tr
                  key={hit.segment_id}
                  className={`is-clickable ${selected?.segment_id === hit.segment_id ? "is-selected" : ""}`}
                  onClick={() => {
                    setSelected(hit);
                    setJumpToken((token) => token + 1);
                  }}
                  onDoubleClick={() => onOpenReview(hit.recording_id, hit.segment_id)}
                >
                  <td className="mono hint">{index + 1}</td>
                  <td className="mono">{formatTimecode(hit.start_s)}</td>
                  {/*
                    Resolved through `recordings` (all cases), not `labels` (open
                    case only). The old `labels[hit.recording_id] ?? "—"` printed a
                    dash for every archive hit — the Exhibit column, the one column
                    whose job is to say which file a passage came from, was blank
                    for exactly the passages the operator had just widened the
                    search to find. `ExhibitLabel` adds the case number for a
                    foreign recording, because `E-002` alone means two different
                    files in two different cases.
                  */}
                  <td className="mono">
                    <ExhibitLabel
                      entry={recordings[hit.recording_id]}
                      qualify={resultScope?.scope === "all"}
                    />
                  </td>
                  <td>
                    <div>{hit.snippet}</div>
                    <div className="hitmeta">
                      {hit.recording_filename} ·{" "}
                      {hit.recorded_at
                        ? new Date(hit.recorded_at).toLocaleDateString()
                        : "date not recorded"}{" "}
                      · {hit.speaker ?? "speaker unknown"}
                    </div>
                  </td>
                  <td>
                    {hit.matched_by.map((signal) => (
                      <span className="sig" key={signal} style={{ marginRight: 3 }}>
                        {signal === "semantic" ? "sem" : signal === "keyword" ? "kw" : signal}
                      </span>
                    ))}
                  </td>
                  <td>
                    <ReviewStamp status={hit.review_status} />
                  </td>
                  <td className="num mono">{hit.score.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <PlayerDock
        recordingId={selected?.recording_id ?? null}
        title={
          selected
            ? `${selectedExhibit?.label ?? ""} · ${selected.recording_filename}${
                resultScope?.scope === "all" && selectedExhibit
                  ? ` (case ${selectedExhibit.caseNumber})`
                  : ""
              }`
            : "No passage selected"
        }
        subtitle={
          selected
            ? `${formatTimecode(selected.start_s)} – ${formatTimecode(selected.end_s)} · ${
                selected.speaker ?? "speaker unknown"
              }`
            : "select a result to load the audio"
        }
        hasDenoised={selectedExhibit?.hasDenoised ?? false}
        jumpTo={selected?.start_s ?? null}
        jumpToken={jumpToken}
        fallbackDuration={selectedExhibit?.duration ?? null}
        actions={
          <button
            className="btn btn--sm btn--primary"
            disabled={!selected}
            onClick={() => selected && onOpenReview(selected.recording_id, selected.segment_id)}
          >
            Open in review
          </button>
        }
      />

    </div>
  );
}
