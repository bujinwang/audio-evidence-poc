import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import AudioPlayer from "../components/AudioPlayer";
import { Notice, ReviewBadge, SourceTag } from "../components/Badges";
import { formatDate, formatTimecode } from "../lib/format";
import type {
  Comparison,
  Provenance,
  Recording,
  Segment,
  SegmentHistory,
  Transcript,
} from "../types";

interface ReviewProps {
  recordingId: string;
  focusSegmentId?: string | null;
  focusStart?: number | null;
  onBack: () => void;
}

export default function Review({ recordingId, focusSegmentId, focusStart, onBack }: ReviewProps) {
  const [recording, setRecording] = useState<Recording | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [jumpTo, setJumpTo] = useState<number | null>(focusStart ?? null);
  const [jumpToken, setJumpToken] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [history, setHistory] = useState<SegmentHistory | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [showComparison, setShowComparison] = useState(false);
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  const [showProvenance, setShowProvenance] = useState(false);
  const [denoising, setDenoising] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selected = useMemo(
    () => segments.find((segment) => segment.id === selectedId) ?? null,
    [segments, selectedId],
  );

  const refreshRecording = useCallback(async () => {
    try {
      setRecording(await api.getRecording(recordingId));
    } catch {
      /* non-fatal */
    }
  }, [recordingId]);

  const refreshHistory = useCallback(async (segmentId: string) => {
    try {
      setHistory(await api.history(segmentId));
    } catch {
      setHistory(null);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [record, tr] = await Promise.all([
          api.getRecording(recordingId),
          api.transcript(recordingId),
        ]);
        if (cancelled) return;
        setRecording(record);
        setTranscript(tr);
        setSegments(tr.segments);

        const initial =
          tr.segments.find((segment) => segment.id === focusSegmentId) ?? tr.segments[0] ?? null;
        if (initial) {
          setSelectedId(initial.id);
          setJumpTo(focusStart ?? initial.start_s);
          setJumpToken((token) => token + 1);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load recording");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [recordingId, focusSegmentId, focusStart]);

  useEffect(() => {
    if (selectedId) void refreshHistory(selectedId);
  }, [selectedId, refreshHistory]);

  useEffect(() => {
    if (!selectedId) return;
    const element = document.getElementById(`segment-${selectedId}`);
    element?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selectedId]);

  const selectSegment = (segment: Segment, seek = true) => {
    setSelectedId(segment.id);
    setDraft(null);
    setNote("");
    setNotice(null);
    if (seek) {
      setJumpTo(segment.start_s);
      setJumpToken((token) => token + 1);
    }
  };

  const saveCorrection = async () => {
    if (!selected || draft === null) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.updateSegment(selected.id, draft, note || undefined);
      setSegments((previous) =>
        previous.map((segment) => (segment.id === updated.id ? updated : segment)),
      );
      setDraft(null);
      setNote("");
      setNotice("Correction saved. Verification is a separate action.");
      await refreshHistory(updated.id);
      await refreshRecording();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the correction");
    } finally {
      setBusy(false);
    }
  };

  const setVerification = async (verified: boolean) => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.verifySegment(selected.id, verified, note || undefined);
      setSegments((previous) =>
        previous.map((segment) => (segment.id === updated.id ? updated : segment)),
      );
      setNote("");
      setNotice(
        verified
          ? `Segment verified by ${updated.verified_by}.`
          : "Verification withdrawn.",
      );
      await refreshHistory(updated.id);
      await refreshRecording();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change verification");
    } finally {
      setBusy(false);
    }
  };

  const reopen = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const updated = await api.reopenSegment(selected.id);
      setSegments((previous) =>
        previous.map((segment) => (segment.id === updated.id ? updated : segment)),
      );
      setNotice("Segment reopened for review. Any verification was withdrawn.");
      await refreshHistory(updated.id);
      await refreshRecording();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reopen the segment");
    } finally {
      setBusy(false);
    }
  };

  const toggleComparison = async () => {
    if (showComparison) {
      setShowComparison(false);
      return;
    }
    if (!comparison) {
      try {
        setComparison(await api.comparison(recordingId));
      } catch {
        setComparison(null);
      }
    }
    setShowComparison(true);
  };

  const runDenoise = async () => {
    setDenoising(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.denoise(recordingId);
      setRecording(updated);
      setNotice("Denoised derivative ready — A/B the player to hear the difference.");
      // Refresh the comparison so the denoised transcript, if produced, is shown.
      setComparison(null);
      if (showComparison) {
        try {
          setComparison(await api.comparison(recordingId));
        } catch {
          setComparison(null);
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not denoise this recording");
    } finally {
      setDenoising(false);
    }
  };

  const toggleProvenance = async () => {
    if (showProvenance) {
      setShowProvenance(false);
      return;
    }
    try {
      setProvenance(await api.provenance(recordingId));
    } catch {
      setProvenance(null);
    }
    setShowProvenance(true);
  };

  const activeId = useMemo(() => {
    const active = segments.find(
      (segment) => currentTime >= segment.start_s && currentTime < segment.end_s,
    );
    return active?.id ?? null;
  }, [segments, currentTime]);

  const verifiedCount = segments.filter((segment) => segment.review_status === "verified").length;
  const correctedCount = segments.filter((segment) => segment.review_status === "corrected").length;

  return (
    <div className="screen screen--review">
      <section className="panel panel--reviewhead">
        <div className="reviewhead">
          <button className="btn btn--ghost btn--small" type="button" onClick={onBack}>
            ← Back
          </button>
          <div className="reviewhead__main">
            <h2>{recording?.filename ?? "Loading…"}</h2>
            <div className="reviewhead__meta">
              {recording && <SourceTag source={recording.source_type} />}
              {recording?.recorded_at && (
                <span className="dim small">{formatDate(recording.recorded_at)}</span>
              )}
              {transcript && (
                <span className="tag tag--engine">
                  {transcript.engine} · {transcript.model_version}
                </span>
              )}
              <span className="dim small">
                {segments.length} segments · {verifiedCount} verified · {correctedCount} corrected
              </span>
            </div>
          </div>
          <div className="reviewhead__actions">
            {recording && !recording.has_denoised && (
              <button
                className="btn btn--primary btn--small"
                type="button"
                onClick={() => void runDenoise()}
                disabled={denoising || busy}
                title="Clean up the noise floor so speech is easier to hear and the recogniser has less to fight"
              >
                {denoising ? "Denoising…" : "✦ Denoise this recording"}
              </button>
            )}
            <button
              className={`btn btn--small ${showComparison ? "btn--primary" : "btn--ghost"}`}
              type="button"
              onClick={() => void toggleComparison()}
            >
              Original vs denoised
            </button>
            <button
              className={`btn btn--small ${showProvenance ? "btn--primary" : "btn--ghost"}`}
              type="button"
              onClick={() => void toggleProvenance()}
            >
              Chain of custody
            </button>
          </div>
        </div>

        {transcript?.is_reference_copy && (
          <Notice tone="warn">
            This transcript is a reference copy, not an independent ASR pass.{" "}
            {String(transcript.settings.note ?? "")}
          </Notice>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        {notice && <Notice tone="info">{notice}</Notice>}
      </section>

      {showProvenance && (
        <section className="panel">
          <header className="panel__head panel__head--tight">
            <div>
              <h2>Chain of custody</h2>
              <p className="muted small">
                Every stage recorded against this file — who did what, and when. The original is
                hashed on receipt and never modified.
              </p>
            </div>
          </header>
          {!provenance ? (
            <Notice tone="info">Custody record is not available for this recording.</Notice>
          ) : (
            <>
              <div className="custody__meta">
                <div>
                  <span className="custody__k">Intake</span>
                  <span className="custody__v">
                    {provenance.intake_mode === "triage"
                      ? "Triaged — registered, not yet indexed"
                      : provenance.intake_mode === "triage_promoted"
                        ? "Triaged on receipt, later indexed for a case"
                        : "Full transcription and indexing on receipt"}
                  </span>
                </div>
                <div>
                  <span className="custody__k">SHA-256</span>
                  <span className="custody__v mono small">{provenance.sha256}</span>
                </div>
              </div>
              <ol className="custody">
                {provenance.steps.map((step, index) => (
                  <li className="custody__step" key={`${step.stage}-${index}`}>
                    <span className="custody__dot" aria-hidden />
                    <div className="custody__body">
                      <div className="custody__label">
                        {step.label}
                        {Number(step.detail?.count ?? 1) > 1 && (
                          <span className="custody__count"> ×{String(step.detail.count)}</span>
                        )}
                      </div>
                      <div className="custody__sub dim small">
                        {step.actor ? `${step.actor}${step.role ? ` (${step.role})` : ""} · ` : ""}
                        {step.at ? formatDate(step.at) : "time not recorded"}
                      </div>
                    </div>
                  </li>
                ))}
                {provenance.steps.length === 0 && (
                  <li className="dim small">No custody events recorded yet.</li>
                )}
              </ol>
            </>
          )}
        </section>
      )}

      {showComparison && (
        <section className="panel">
          <header className="panel__head panel__head--tight">
            <div>
              <h2>Original vs denoised</h2>
              <p className="muted small">
                Denoising is never assumed to improve recognition. The original transcript stays
                the reference.
              </p>
            </div>
          </header>
          {!comparison || !comparison.available ? (
            <Notice tone="info">
              {comparison?.reason ?? "Comparison is not available for this recording."}
            </Notice>
          ) : (
            <>
              {comparison.denoised_is_reference_copy && (
                <Notice tone="warn">
                  The denoised transcript is a reference copy — this comparison shows no
                  recognition difference.
                </Notice>
              )}
              <div className="comparison">
                <div className="comparison__head">
                  <span>Time</span>
                  <span>Original ({comparison.original_engine})</span>
                  <span>Denoised ({comparison.denoised_engine})</span>
                  <span>Similarity</span>
                </div>
                {comparison.rows.map((row) => (
                  <div className="comparison__row" key={row.idx}>
                    <span className="mono small">{formatTimecode(row.start_s)}</span>
                    <span>{row.original_text}</span>
                    <span className="dim">{row.denoised_text || "—"}</span>
                    <span className="mono small">
                      {row.similarity === null ? "—" : row.similarity.toFixed(2)}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>
      )}

      <div className="review-layout">
        <section className="panel panel--transcript">
          <header className="panel__head panel__head--tight">
            <h2>Transcript</h2>
            <span className="dim small">Click a line to seek the recording</span>
          </header>

          <div className="transcript">
            {segments.map((segment) => {
              const isActive = segment.id === activeId;
              const isSelected = segment.id === selectedId;
              return (
                <button
                  key={segment.id}
                  id={`segment-${segment.id}`}
                  type="button"
                  className={`line ${isSelected ? "is-selected" : ""} ${isActive ? "is-playing" : ""}`}
                  onClick={() => selectSegment(segment)}
                >
                  <span className="line__time mono">{formatTimecode(segment.start_s)}</span>
                  <span className="line__body">
                    <span className="line__speaker">
                      {segment.speaker ?? "unlabelled"}
                      {segment.edited && <span className="line__edited">edited</span>}
                    </span>
                    <span className="line__text">{segment.text || <em>empty</em>}</span>
                  </span>
                  <span className="line__status">
                    <ReviewBadge status={segment.review_status} />
                  </span>
                </button>
              );
            })}
            {segments.length === 0 && <div className="empty">No transcript for this recording.</div>}
          </div>
        </section>

        <aside className="panel panel--detail">
          <AudioPlayer
            src={api.audioUrl(recordingId)}
            altSrc={recording?.has_denoised ? api.audioUrl(recordingId, "denoised") : null}
            altLabel="Denoised"
            primaryLabel="Original"
            jumpTo={jumpTo}
            jumpToken={jumpToken}
            onTime={setCurrentTime}
            title="Original recording"
            subtitle={
              recording?.has_denoised
                ? "Switch between the original and the denoised derivative at the same position"
                : "Playback always uses the unmodified source file"
            }
            compact
          />

          {selected ? (
            <>
              <div className="detail__head">
                <div>
                  <div className="detail__time mono">
                    {formatTimecode(selected.start_s)} – {formatTimecode(selected.end_s)}
                  </div>
                  <div className="dim small">
                    {selected.speaker ?? "unlabelled"} · v{selected.version}
                  </div>
                </div>
                <ReviewBadge status={selected.review_status} />
              </div>

              {draft === null ? (
                <div className="detail__text">{selected.text || <em>empty segment</em>}</div>
              ) : (
                <div className="editor">
                  <textarea
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    rows={5}
                    aria-label="Corrected transcript text"
                  />
                  <input
                    className="input"
                    placeholder="Reason for the correction (recorded in the audit trail)"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                  />
                  <div className="editor__actions">
                    <button
                      className="btn btn--primary btn--small"
                      type="button"
                      onClick={() => void saveCorrection()}
                      disabled={busy}
                    >
                      Save correction
                    </button>
                    <button
                      className="btn btn--ghost btn--small"
                      type="button"
                      onClick={() => {
                        setDraft(null);
                        setNote("");
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {selected.edited && (
                <div className="ai-original">
                  <div className="ai-original__label">AI generated (preserved)</div>
                  <p>{selected.ai_text}</p>
                </div>
              )}

              <div className="detail__actions">
                {draft === null && (
                  <button
                    className="btn btn--small"
                    type="button"
                    onClick={() => {
                      setDraft(selected.text);
                      setNote("");
                    }}
                  >
                    Correct text
                  </button>
                )}
                {selected.review_status !== "verified" ? (
                  <button
                    className="btn btn--primary btn--small"
                    type="button"
                    onClick={() => void setVerification(true)}
                    disabled={busy}
                  >
                    Mark verified
                  </button>
                ) : (
                  <button
                    className="btn btn--ghost btn--small"
                    type="button"
                    onClick={() => void setVerification(false)}
                    disabled={busy}
                  >
                    Withdraw verification
                  </button>
                )}
                <button
                  className="btn btn--ghost btn--small"
                  type="button"
                  onClick={() => void reopen()}
                  disabled={busy}
                >
                  Reopen
                </button>
              </div>

              {selected.verified_by && (
                <p className="dim small">
                  Verified by {selected.verified_by} on {formatDate(selected.verified_at)}
                </p>
              )}

              <div className="history">
                <div className="history__head">History</div>
                {history && history.revisions.length === 0 && history.events.length === 0 && (
                  <p className="dim small">No changes recorded yet.</p>
                )}
                <ul className="history__list">
                  {history?.revisions.map((revision) => (
                    <li key={revision.id} className="history__item">
                      <div className="history__row">
                        <span className="mono small">v{revision.version}</span>
                        <span className="small">{revision.actor}</span>
                        <span className="dim small">{formatDate(revision.created_at)}</span>
                      </div>
                      <div className="history__diff">
                        <span className="history__prev">{revision.prev_text || "—"}</span>
                        <span className="history__arrow">→</span>
                        <span className="history__next">{revision.new_text || "—"}</span>
                      </div>
                      {revision.note && <div className="dim small">“{revision.note}”</div>}
                    </li>
                  ))}
                  {history?.events
                    .filter((event) => event.action !== "segment.edited")
                    .map((event) => (
                      <li key={event.id} className="history__item history__item--event">
                        <div className="history__row">
                          <span className="mono small">{event.action}</span>
                          <span className="small">{event.actor}</span>
                          <span className="dim small">{formatDate(event.ts)}</span>
                        </div>
                      </li>
                    ))}
                </ul>
              </div>
            </>
          ) : (
            <div className="empty">Select a transcript line to review it.</div>
          )}
        </aside>
      </div>
    </div>
  );
}
