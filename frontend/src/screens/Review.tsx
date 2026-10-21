import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Empty, Notice, Panel } from "../components/Layout";
import { ConfidenceChip, ReviewStamp, Stamp } from "../components/Stamp";
import CustodyList from "../components/CustodyList";
import type { CustodyEvent } from "../components/CustodyList";
import PlayerDock from "../components/PlayerDock";
import { formatDate, formatTimecode } from "../lib/format";
import type { CaseFile, Cue, CueDecision, Provenance, Segment, SegmentHistory } from "../types";

export default function Review({
  caseFile,
  recordingId,
  focusSegmentId,
  onSelectRecording,
  onChanged,
}: {
  caseFile: CaseFile;
  recordingId: string | null;
  focusSegmentId?: string | null;
  onSelectRecording: (recordingId: string) => void;
  onChanged: () => Promise<void> | void;
}) {
  const indexed = useMemo(
    () => caseFile.exhibits.filter((exhibit) => exhibit.indexed !== "none"),
    [caseFile.exhibits],
  );

  // Opening Review with no exhibit in the URL lands on the first indexed one.
  useEffect(() => {
    if (!recordingId && indexed[0]) onSelectRecording(indexed[0].id);
  }, [recordingId, indexed, onSelectRecording]);

  const [segments, setSegments] = useState<Segment[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [cues, setCues] = useState<Cue[]>([]);
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  const [history, setHistory] = useState<SegmentHistory | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [jumpTo, setJumpTo] = useState<number | null>(null);
  const [jumpToken, setJumpToken] = useState(0);
  const [needsReviewOnly, setNeedsReviewOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deciding, setDeciding] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const exhibitIndex = caseFile.exhibits.findIndex((exhibit) => exhibit.id === recordingId);
  const exhibit = caseFile.exhibits[exhibitIndex];
  const label = exhibitIndex >= 0 ? `E-${String(exhibitIndex + 1).padStart(3, "0")}` : "—";

  const loadCues = useCallback(async () => {
    if (!recordingId) return;
    try {
      setCues(await api.cues(caseFile.id, { recording_id: recordingId }));
    } catch {
      setCues([]);
    }
  }, [caseFile.id, recordingId]);

  useEffect(() => {
    if (!recordingId) return;
    let cancelled = false;
    setError(null);
    setDraft(null);
    (async () => {
      try {
        const transcript = await api.transcript(recordingId);
        if (cancelled) return;
        setSegments(transcript.segments);
        const focus =
          transcript.segments.find((segment) => segment.id === focusSegmentId) ??
          transcript.segments[0] ??
          null;
        setSelectedId(focus?.id ?? null);
        setJumpTo(focus?.start_s ?? null);
        setJumpToken((token) => token + 1);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load the transcript");
      }
    })();
    void loadCues();
    api.provenance(recordingId).then(setProvenance).catch(() => setProvenance(null));
    return () => {
      cancelled = true;
    };
  }, [recordingId, focusSegmentId, loadCues]);

  const selected = useMemo(
    () => segments.find((segment) => segment.id === selectedId) ?? null,
    [segments, selectedId],
  );

  useEffect(() => {
    if (!selectedId) return;
    api.history(selectedId).then(setHistory).catch(() => setHistory(null));
  }, [selectedId, segments]);

  const visible = needsReviewOnly
    ? segments.filter((segment) => segment.review_status === "ai_generated")
    : segments;

  const select = (segment: Segment) => {
    setSelectedId(segment.id);
    setDraft(null);
    setNote("");
    setNotice(null);
    setJumpTo(segment.start_s);
    setJumpToken((token) => token + 1);
  };

  const saveCorrection = async () => {
    if (!selected || draft === null) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.updateSegment(selected.id, draft, note || undefined);
      setSegments((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      setDraft(null);
      setNote("");
      setNotice("Correction saved. Verification is a separate action.");
      await onChanged();
      await loadCues();
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
      setSegments((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      setNote("");
      setNotice(verified ? `Segment verified by ${updated.verified_by}.` : "Verification withdrawn.");
      await onChanged();
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
      setSegments((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      setNotice("Segment reopened for review. Any verification was withdrawn.");
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reopen the segment");
    } finally {
      setBusy(false);
    }
  };

  // A cue decision is a small write but an auditable one: a double click used to
  // post twice and produce two audit entries for one decision. Guard per cue so
  // deciding one span never blocks the others, and ignore a repeat while the
  // first request is still in flight.
  const decide = async (cue: Cue, decision: CueDecision) => {
    if (deciding.has(cue.id)) return;
    if (cue.decision === decision) return;
    setDeciding((previous) => new Set(previous).add(cue.id));
    setError(null);
    try {
      const updated = await api.decideCue(cue.id, decision);
      setCues((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the decision");
    } finally {
      setDeciding((previous) => {
        const next = new Set(previous);
        next.delete(cue.id);
        return next;
      });
    }
  };

  const segmentCues = cues.filter((cue) => cue.segment_id === selectedId);
  const openCues = cues.filter((cue) => cue.decision === "open").length;

  const events: CustodyEvent[] =
    provenance?.steps
      .slice()
      .reverse()
      .slice(0, 4)
      .map((step) => ({
        label: step.label,
        detail: [step.actor, step.at ? formatDate(step.at) : null].filter(Boolean).join(" · "),
      })) ?? [];

  if (!exhibit) {
    return (
      <div className="view">
        <Empty>
          No indexed exhibit in this case yet. Promote a recording from Evidence, then review it.
        </Empty>
      </div>
    );
  }

  return (
    <div className="view" style={{ paddingBottom: 64 }}>
      <div className="reviewbar">
        <div className="reviewbar__left">
          <select
            className="btn btn--sm"
            value={recordingId ?? ""}
            onChange={(event) => onSelectRecording(event.target.value)}
            aria-label="Exhibit"
            style={{ maxWidth: 320 }}
          >
            {indexed.map((item) => (
              <option key={item.id} value={item.id}>
                E-{String(caseFile.exhibits.indexOf(item) + 1).padStart(3, "0")} · {item.filename}
              </option>
            ))}
          </select>
          {selected && (
            <span className="mono hint">
              segment {segments.findIndex((item) => item.id === selected.id) + 1} of {segments.length}
            </span>
          )}
        </div>
        <div className="reviewbar__right">
          {selected && <ReviewStamp status={selected.review_status} />}
          <button
            className={`chip ${needsReviewOnly ? "is-on" : ""}`}
            onClick={() => setNeedsReviewOnly((value) => !value)}
            title="Show only segments nobody has reviewed"
          >
            Needs review
          </button>
        </div>
      </div>

      {error && (
        <div style={{ padding: "10px 24px 0" }}>
          <Notice tone="error">{error}</Notice>
        </div>
      )}
      {notice && (
        <div style={{ padding: "10px 24px 0" }}>
          <Notice tone="info">{notice}</Notice>
        </div>
      )}

      <div className="review">
        <div className="review__list">
          <div className="review__listhead">
            <span>Transcript</span>
            <span>{visible.length} segments</span>
          </div>
          {visible.length === 0 && <Empty>Nothing in this filter.</Empty>}
          {visible.map((segment) => (
            <button
              key={segment.id}
              data-segment-id={segment.id}
              className={`seg-row ${segment.id === selectedId ? "is-active" : ""}`}
              onClick={() => select(segment)}
            >
              <span className="mono">{formatTimecode(segment.start_s)}</span>
              <span className="seg-row__text">
                {segment.text || <em>empty</em>}
                {segment.edited && <span className="hint"> · edited</span>}
              </span>
              <span
                className={`conf ${
                  segment.review_status === "verified"
                    ? "conf--high"
                    : segment.review_status === "corrected"
                      ? "conf--mid"
                      : "conf--low"
                }`}
                title={segment.review_status}
              >
                {segment.review_status === "verified"
                  ? "✓"
                  : segment.review_status === "corrected"
                    ? "✎"
                    : "•"}
              </span>
            </button>
          ))}
        </div>

        <div className="review__main">
          {!selected ? (
            <Empty>Select a transcript line to review it.</Empty>
          ) : (
            <>
              <div className="review__meta">
                <span className="mono">
                  {formatTimecode(selected.start_s)} – {formatTimecode(selected.end_s)}
                </span>
                <span className="hint">·</span>
                <span className="hint">
                  {selected.speaker ?? "speaker unknown"} · v{selected.version}
                </span>
                <span className="hint" style={{ marginLeft: "auto" }}>
                  {segmentCues.length} redaction candidate{segmentCues.length === 1 ? "" : "s"}
                </span>
              </div>

              <div className="review__text">
                {selected.text || <em>empty segment</em>}
              </div>

              <div className="review__actions">
                <button
                  className="btn btn--sm"
                  onClick={() => {
                    setDraft(selected.text);
                    setNote("");
                  }}
                  disabled={draft !== null}
                >
                  Correct text
                </button>
                <button
                  className="btn btn--sm"
                  onClick={() => {
                    setJumpTo(selected.start_s);
                    setJumpToken((token) => token + 1);
                  }}
                >
                  Play passage
                </button>
                {selected.review_status !== "verified" ? (
                  <button
                    className="btn btn--sm btn--primary"
                    onClick={() => void setVerification(true)}
                    disabled={busy}
                  >
                    Mark verified
                  </button>
                ) : (
                  <button
                    className="btn btn--sm"
                    onClick={() => void setVerification(false)}
                    disabled={busy}
                  >
                    Withdraw verification
                  </button>
                )}
                <button className="btn btn--sm btn--quiet" onClick={() => void reopen()} disabled={busy}>
                  Reopen
                </button>
              </div>

              {draft !== null && (
                <div className="panel" style={{ marginTop: 14 }}>
                  <div className="panel__head">
                    <h2>Correction</h2>
                    <span className="right hint">reason is recorded in the audit trail</span>
                  </div>
                  <div className="panel__body">
                    <div className="diff__row">
                      <span className="diff__k">machine</span>
                      <span className="diff__v--old">{selected.ai_text}</span>
                    </div>
                    <label className="field" style={{ marginTop: 8 }}>
                      <span>Corrected text</span>
                      <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} />
                    </label>
                    <label className="field" style={{ marginTop: 8 }}>
                      <span>Reason (required for the audit trail)</span>
                      <input
                        value={note}
                        onChange={(event) => setNote(event.target.value)}
                        placeholder="misheard name, verified against the audio at 00:32"
                      />
                    </label>
                    <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
                      <button
                        className="btn btn--sm btn--primary"
                        onClick={() => void saveCorrection()}
                        disabled={busy || draft === selected.text}
                      >
                        Save correction
                      </button>
                      <button
                        className="btn btn--sm btn--quiet"
                        onClick={() => {
                          setDraft(null);
                          setNote("");
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {selected.verified_by && (
                <p className="hint" style={{ marginTop: 10 }}>
                  Verified by {selected.verified_by} on {formatDate(selected.verified_at)}
                </p>
              )}

              <div className="panel" style={{ marginTop: 14 }}>
                <div className="panel__head">
                  <h2>History</h2>
                  <span className="right hint">append-only</span>
                </div>
                {history && history.revisions.length === 0 && history.events.length === 0 ? (
                  <div className="panel__body">
                    <p className="hint" style={{ margin: 0 }}>
                      No changes recorded yet. The machine transcript stands as produced.
                    </p>
                  </div>
                ) : (
                  <table className="tbl">
                    <tbody>
                      {history?.revisions.map((revision) => (
                        <tr key={revision.id}>
                          <td className="mono" style={{ width: 44 }}>
                            v{revision.version}
                          </td>
                          <td style={{ width: 130 }}>{revision.actor}</td>
                          <td>
                            <span className="hint">{revision.prev_text || "—"}</span> →{" "}
                            {revision.new_text || "—"}
                            {revision.note && <div className="hint">“{revision.note}”</div>}
                          </td>
                          <td className="num hint" style={{ width: 110 }}>
                            {formatDate(revision.created_at)}
                          </td>
                        </tr>
                      ))}
                      {history?.events
                        .filter((event) => event.action !== "segment.edited")
                        .slice(0, 5)
                        .map((event) => (
                          <tr key={event.id}>
                            <td className="mono hint">·</td>
                            <td>{event.actor}</td>
                            <td className="hint">{event.action}</td>
                            <td className="num hint">{formatDate(event.ts)}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                )}
              </div>
            </>
          )}
        </div>

        <aside className="review__aside">
          <Panel
            title="Redaction"
            right={<span className="hint">{segmentCues.length} here · {openCues} open in exhibit</span>}
            flush
          >
            {segmentCues.length === 0 && (
              <p className="hint" style={{ padding: "12px 14px", margin: 0 }}>
                No candidates in this segment. Cues are rule-detected from the working text.
              </p>
            )}
            {segmentCues.map((cue) => (
              <div className="cue" key={cue.id} data-cue-id={cue.id}>
                <div className="cue__top">
                  <Stamp tone="info">{cue.entity_type}</Stamp>
                  <span className="hint mono">{formatTimecode(cue.start_s)}</span>
                </div>
                <div className="cue__text">“{cue.span_text}”</div>
                <div className="cue__row">
                  <span className="hint">Reason</span>
                  <span className="mono" style={{ fontSize: 11 }}>
                    {cue.reason_code}
                  </span>
                  <span style={{ marginLeft: "auto" }}>
                    <ConfidenceChip value={cue.confidence} />
                  </span>
                </div>
                <div className="cue__actions">
                  <button
                    className={`btn btn--sm ${cue.decision === "accept" ? "is-on" : ""}`}
                    disabled={deciding.has(cue.id)}
                    aria-busy={deciding.has(cue.id)}
                    onClick={() => void decide(cue, "accept")}
                  >
                    Accept
                  </button>
                  <button
                    className={`btn btn--sm ${cue.decision === "reject" ? "is-on-reject" : ""}`}
                    disabled={deciding.has(cue.id)}
                    aria-busy={deciding.has(cue.id)}
                    onClick={() => void decide(cue, "reject")}
                  >
                    Reject
                  </button>
                  <button
                    className={`btn btn--sm btn--quiet ${
                      cue.decision === "not_redactable" ? "is-on" : ""
                    }`}
                    disabled={deciding.has(cue.id)}
                    aria-busy={deciding.has(cue.id)}
                    onClick={() => void decide(cue, "not_redactable")}
                  >
                    Not redactable
                  </button>
                </div>
                {cue.decision !== "open" && (
                  <div className="hint" style={{ marginTop: 5 }}>
                    {cue.decision.replace("_", " ")} · {cue.reviewer}
                  </div>
                )}
              </div>
            ))}
          </Panel>

          <div style={{ marginTop: 14 }}>
            <Panel title="Custody" flush>
              <CustodyList events={events} />
            </Panel>
          </div>
        </aside>
      </div>

      <PlayerDock
        recordingId={recordingId}
        title={`${label} · ${exhibit.filename}`}
        subtitle={
          selected
            ? `${formatTimecode(selected.start_s)} – ${formatTimecode(selected.end_s)}`
            : undefined
        }
        hasDenoised={exhibit.has_denoised}
        jumpTo={jumpTo}
        jumpToken={jumpToken}
        fallbackDuration={exhibit.duration_s}
        actions={
          <button className="btn btn--sm" onClick={() => void onChanged()}>
            Refresh case
          </button>
        }
      />
    </div>
  );
}
