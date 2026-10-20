import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import AudioPlayer from "../components/AudioPlayer";
import { Notice, ReviewBadge, SignalTag, SourceTag } from "../components/Badges";
import { formatDate, formatTime, formatTimecode, highlightTerms } from "../lib/format";
import type { SearchHit, SearchMode, SearchOptions, SearchResponse } from "../types";

interface SearchProps {
  onOpen: (recordingId: string, segmentId: string, start: number) => void;
}

const SUGGESTIONS = [
  "blue pickup truck near a community centre",
  "registration check",
  "green duffel bag",
  "cracked rear window",
];

const STOPWORDS = new Set([
  "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "near", "with",
  "for", "was", "were", "is", "are", "that", "this", "it", "i", "he", "she",
  "they", "we", "you", "me", "my", "his", "her", "their", "what", "when",
  "where", "which", "who", "about", "some", "any", "did", "do",
]);

function termsFromQuery(query: string): string[] {
  return query
    .toLowerCase()
    .split(/[^a-z0-9']+/)
    .filter((token) => token.length > 2 && !STOPWORDS.has(token));
}

export default function Search({ onOpen }: SearchProps) {
  const [query, setQuery] = useState("blue pickup truck near a community centre");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [options, setOptions] = useState<SearchOptions | null>(null);
  const [selected, setSelected] = useState<SearchHit | null>(null);
  const [jumpToken, setJumpToken] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recordingFilter, setRecordingFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [reviewFilter, setReviewFilter] = useState("");

  useEffect(() => {
    api.searchOptions().then(setOptions).catch(() => setOptions(null));
  }, []);

  const run = useCallback(
    async (searchQuery: string, searchMode: SearchMode) => {
      if (!searchQuery.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const response = await api.search({
          q: searchQuery,
          mode: searchMode,
          recording_id: recordingFilter ? [recordingFilter] : undefined,
          source_type: sourceFilter ? [sourceFilter] : undefined,
          review_status: reviewFilter ? [reviewFilter] : undefined,
          limit: 25,
        });
        setResults(response);
        if (response.hits.length > 0) {
          setSelected(response.hits[0]);
          setJumpToken((token) => token + 1);
        } else {
          setSelected(null);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Search failed");
      } finally {
        setLoading(false);
      }
    },
    [recordingFilter, sourceFilter, reviewFilter],
  );

  const terms = useMemo(() => termsFromQuery(results?.query ?? query), [results?.query, query]);

  // Run the demo query once on mount so the screen is never a blank page.
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void run(query, mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    void run(query, mode);
  };

  const changeMode = (next: SearchMode) => {
    setMode(next);
    if (results) void run(query, next);
  };

  return (
    <div className="screen screen--search">
      <section className="panel panel--search">
        <form className="searchbar" onSubmit={submit}>
          <input
            className="searchbar__input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Describe what you are looking for…"
            aria-label="Search query"
          />
          <button className="btn btn--primary" type="submit" disabled={loading}>
            {loading ? "Searching…" : "Search"}
          </button>
        </form>

        <div className="searchbar__row">
          <div className="segmented" role="tablist" aria-label="Search mode">
            {(["hybrid", "keyword", "semantic"] as SearchMode[]).map((value) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={mode === value}
                className={`segmented__item ${mode === value ? "is-active" : ""}`}
                onClick={() => changeMode(value)}
              >
                {value === "hybrid" ? "Hybrid" : value === "keyword" ? "Keyword" : "Semantic"}
              </button>
            ))}
          </div>

          <div className="filters">
            <select
              className="select"
              value={recordingFilter}
              onChange={(event) => setRecordingFilter(event.target.value)}
              aria-label="Filter by recording"
            >
              <option value="">All recordings</option>
              {options?.recordings.map((recording) => (
                <option key={recording.id} value={recording.id}>
                  {recording.filename}
                </option>
              ))}
            </select>

            <select
              className="select"
              value={sourceFilter}
              onChange={(event) => setSourceFilter(event.target.value)}
              aria-label="Filter by source type"
            >
              <option value="">All sources</option>
              {options?.source_types.map((source) => (
                <option key={source} value={source}>
                  {source}
                </option>
              ))}
            </select>

            <select
              className="select"
              value={reviewFilter}
              onChange={(event) => setReviewFilter(event.target.value)}
              aria-label="Filter by review status"
            >
              <option value="">Any review status</option>
              <option value="ai_generated">AI generated</option>
              <option value="in_review">In review</option>
              <option value="corrected">Corrected</option>
              <option value="verified">Verified</option>
            </select>
          </div>
        </div>

        {!results && !loading && (
          <div className="suggestions">
            <span className="dim small">Try:</span>
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
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
        )}

        {error && <Notice tone="error">{error}</Notice>}
      </section>

      <div className="search-layout">
        <section className="panel panel--results">
          <header className="panel__head panel__head--tight">
            <div>
              <h2>Results</h2>
              {results && (
                <p className="muted small">
                  {results.total} passage{results.total === 1 ? "" : "s"} · {results.took_ms} ms ·{" "}
                  {results.mode}
                  {results.diagnostics.embedder
                    ? ` · ${results.diagnostics.embedder}`
                    : ""}
                  {results.diagnostics.semantic === false ? " (lexical fallback)" : ""}
                </p>
              )}
            </div>
          </header>

          {loading && <div className="empty">Retrieving passages…</div>}
          {!loading && !results && (
            <div className="empty">Enter a query to search the transcript index.</div>
          )}
          {!loading && results && results.hits.length === 0 && (
            <div className="empty">No passages matched. Try a different phrase or mode.</div>
          )}

          <ol className="hits">
            {results?.hits.map((hit, index) => (
              <li key={hit.segment_id}>
                <button
                  type="button"
                  className={`hit ${selected?.segment_id === hit.segment_id ? "is-active" : ""}`}
                  onClick={() => {
                    setSelected(hit);
                    setJumpToken((token) => token + 1);
                  }}
                >
                  <div className="hit__top">
                    <span className="hit__rank">{index + 1}</span>
                    <span className="hit__time mono">{formatTimecode(hit.start_s)}</span>
                    <span className="hit__file" title={hit.recording_filename}>
                      {hit.recording_filename}
                    </span>
                    <span className="hit__spacer" />
                    <ReviewBadge status={hit.review_status} />
                  </div>

                  <div className="hit__text">
                    {highlightTerms(hit.snippet, terms).map((part, partIndex) =>
                      part.match ? (
                        <mark key={partIndex}>{part.text}</mark>
                      ) : (
                        <span key={partIndex}>{part.text}</span>
                      ),
                    )}
                  </div>

                  <div className="hit__meta">
                    <SourceTag source={hit.source_type} />
                    {hit.speaker && <span className="tag tag--speaker">{hit.speaker}</span>}
                    {hit.matched_by.map((signal) => (
                      <SignalTag key={signal} signal={signal} />
                    ))}
                    <span className="dim small">{formatDate(hit.recorded_at)}</span>
                    <span className="hit__score mono dim">
                      {hit.score.toFixed(3)}
                    </span>
                  </div>
                </button>
              </li>
            ))}
          </ol>
        </section>

        <aside className="panel panel--player">
          <AudioPlayer
            src={selected ? api.audioUrl(selected.recording_id) : null}
            jumpTo={selected?.start_s ?? null}
            jumpToken={jumpToken}
            title={selected?.recording_filename ?? "No passage selected"}
            subtitle={
              selected
                ? `${formatTime(selected.start_s)} – ${formatTime(selected.end_s)} · ${
                    selected.speaker ?? "speaker unknown"
                  }`
                : undefined
            }
          />

          {selected && (
            <div className="passage">
              <div className="passage__label">Passage</div>
              <p className="passage__text">{selected.text}</p>
              <div className="passage__meta">
                <ReviewBadge status={selected.review_status} />
                <span className="mono dim small">{selected.recording_id}</span>
              </div>
              <button
                className="btn btn--primary btn--block"
                type="button"
                onClick={() => onOpen(selected.recording_id, selected.segment_id, selected.start_s)}
              >
                Open in review
              </button>
              <p className="disclaimer">
                Search relevance is not evidence of truth. Verify against the original recording.
              </p>
            </div>
          )}

          {!selected && (
            <div className="empty">
              Results appear here. Selecting a passage jumps the original recording to its
              timestamp.
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
