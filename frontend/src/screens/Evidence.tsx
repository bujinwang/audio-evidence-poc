import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Bar, Empty, MetaStrip, Notice, Panel } from "../components/Layout";
import { Stamp, SourceTag } from "../components/Stamp";
import CustodyList from "../components/CustodyList";
import type { CustodyEvent } from "../components/CustodyList";
import { daysLabel, formatBytes, formatDuration, formatHours } from "../lib/format";
import type { CaseFile, Exhibit, Provenance } from "../types";

const SOURCE_TYPES = [
  { value: "interview", label: "Interview" },
  { value: "bodycam", label: "Body camera" },
  { value: "cassette", label: "Cassette transfer" },
  { value: "phone_call", label: "Phone call" },
  { value: "sports", label: "Sporting event" },
  { value: "speech", label: "Public speech" },
  { value: "other", label: "Other" },
];

export default function Evidence({
  caseFile,
  onOpenReview,
  onOpenBundle,
  onChanged,
}: {
  caseFile: CaseFile;
  onOpenReview: (recordingId: string) => void;
  onOpenBundle: () => void;
  onChanged: () => Promise<void> | void;
}) {
  const [selected, setSelected] = useState<Exhibit | null>(caseFile.exhibits[0] ?? null);
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!selected && caseFile.exhibits[0]) setSelected(caseFile.exhibits[0]);
  }, [caseFile, selected]);

  useEffect(() => {
    if (!selected) return;
    api
      .provenance(selected.id)
      .then(setProvenance)
      .catch(() => setProvenance(null));
  }, [selected]);

  const events: CustodyEvent[] =
    provenance?.steps
      .slice()
      .reverse()
      .slice(0, 5)
      .map((step) => ({
        label: step.label,
        detail: [step.actor, step.at ? new Date(step.at).toLocaleString() : null]
          .filter(Boolean)
          .join(" · "),
      })) ?? [];

  const promote = async (exhibit: Exhibit) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.retry(exhibit.id);
      setNotice(`${exhibit.filename} queued for indexing.`);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not promote the recording");
    } finally {
      setBusy(false);
    }
  };

  const dark = caseFile.exhibits.filter((exhibit) => exhibit.indexed === "none");

  return (
    <div className="view">
      <div className="casehead">
        <div className="casehead__top">
          <h1>{caseFile.subject}</h1>
          <span className="casehead__no">{caseFile.number}</span>
          <Stamp tone={caseFile.days_to_disclosure !== null && caseFile.days_to_disclosure <= 3 ? "flagged" : "info"}>
            {caseFile.days_to_disclosure !== null && caseFile.days_to_disclosure <= 3
              ? `Due ${daysLabel(caseFile.days_to_disclosure)}`
              : "Crown disclosure"}
          </Stamp>
        </div>
        <p className="casehead__sub">{caseFile.description ?? "No description recorded."}</p>
        <div className="casehead__actions">
          <button className="btn btn--primary" onClick={() => setAdding((value) => !value)}>
            {adding ? "Close upload" : "Add a recording"}
          </button>
          <button
            className="btn"
            onClick={() => selected && onOpenReview(selected.id)}
            disabled={!selected || selected.indexed === "none"}
          >
            Continue review
          </button>
          <button className="btn" onClick={onOpenBundle}>
            Build disclosure
          </button>
        </div>
      </div>

      <MetaStrip
        items={[
          { k: "Exhibits", v: caseFile.exhibits.length },
          { k: "Audio", v: formatHours(caseFile.totals.audio_hours) },
          {
            k: "Indexed",
            v: (
              <>
                {formatHours(caseFile.totals.indexed_hours)}{" "}
                <span className="hint">of {formatHours(caseFile.totals.audio_hours)}</span>
              </>
            ),
          },
          { k: "Reviewed", v: `${caseFile.totals.reviewed_pct}%` },
          {
            k: "Cue candidates",
            v: (
              <>
                {caseFile.totals.cue_total}{" "}
                <span className="hint">· {caseFile.totals.cue_open} open</span>
              </>
            ),
          },
          {
            k: "Unindexed",
            v: dark.length ? `${dark.length} exhibit${dark.length === 1 ? "" : "s"}` : "none",
            tone: dark.length ? "var(--amber)" : undefined,
          },
        ]}
      />

      {error && (
        <div className="grid">
          <Notice tone="error">{error}</Notice>
        </div>
      )}
      {notice && (
        <div className="grid">
          <Notice tone="info">{notice}</Notice>
        </div>
      )}

      {adding && (
        <div className="grid">
          <UploadPanel
            caseId={caseFile.id}
            onDone={async (recordingId) => {
              setAdding(false);
              await onChanged();
              if (recordingId) onOpenReview(recordingId);
            }}
          />
        </div>
      )}

      <div className="grid">
        <div className="panel">
          <div className="panel__head">
            <h2>Exhibit list</h2>
            <span className="right hint">
              Exhibit numbers follow the recording date · click a row to inspect custody
            </span>
          </div>
          {caseFile.exhibits.length === 0 ? (
            <Empty>No exhibits yet. Add a recording to start the case file.</Empty>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th style={{ width: 64 }}>Exhibit</th>
                  <th>Recording</th>
                  <th style={{ width: 82 }}>Source</th>
                  <th className="num" style={{ width: 70 }}>
                    Length
                  </th>
                  <th style={{ width: 178 }}>Indexed</th>
                  <th style={{ width: 116 }}>Reviewed</th>
                  <th style={{ width: 96 }}>Custody</th>
                  <th style={{ width: 96 }} />
                </tr>
              </thead>
              <tbody>
                {caseFile.exhibits.map((exhibit, index) => {
                  const label = `E-${String(index + 1).padStart(3, "0")}`;
                  return (
                    <tr
                      key={exhibit.id}
                      className={`is-clickable ${selected?.id === exhibit.id ? "is-selected" : ""}`}
                      onClick={() => setSelected(exhibit)}
                    >
                      <td className="mono">{label}</td>
                      <td>
                        <div className="subject">{exhibit.filename}</div>
                        <div className="sub">
                          {exhibit.recorded_at
                            ? new Date(exhibit.recorded_at).toLocaleDateString()
                            : "date not recorded"}{" "}
                          · {exhibit.segment_count} segments
                          {exhibit.cue_total > 0 ? ` · ${exhibit.cue_total} cue candidates` : ""}
                        </div>
                      </td>
                      <td>
                        <SourceTag source={exhibit.source_type} />
                      </td>
                      <td className="num mono">{formatDuration(exhibit.duration_s)}</td>
                      <td>
                        {exhibit.indexed === "full" ? (
                          <>
                            <Stamp tone="verified">Full</Stamp>{" "}
                            <span className="hint">{exhibit.segment_count} seg</span>
                          </>
                        ) : (
                          <>
                            <Stamp tone="unreviewed">None</Stamp>{" "}
                            <span className="hint">{formatDuration(exhibit.dark_s)} dark</span>
                          </>
                        )}
                      </td>
                      <td>
                        <div style={{ marginBottom: 3 }}>
                          <Bar
                            value={exhibit.reviewed_pct}
                            tone={exhibit.reviewed_pct >= 100 ? "green" : "amber"}
                          />
                        </div>
                        <span className="hint tnum">{exhibit.reviewed_pct}%</span>
                      </td>
                      <td className="hint tnum">{exhibit.custody_events}</td>
                      <td>
                        {exhibit.indexed === "none" ? (
                          <button
                            className="btn btn--sm"
                            disabled={busy}
                            onClick={(event) => {
                              event.stopPropagation();
                              void promote(exhibit);
                            }}
                          >
                            Promote
                          </button>
                        ) : (
                          <button
                            className="btn btn--sm"
                            onClick={(event) => {
                              event.stopPropagation();
                              onOpenReview(exhibit.id);
                            }}
                          >
                            Open
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1.15fr 1fr", gap: 14 }}>
          <Panel
            title={
              selected
                ? `Coverage — E-${String(caseFile.exhibits.indexOf(selected) + 1).padStart(3, "0")}`
                : "Coverage"
            }
            right={<span className="hint">what a search can and cannot reach</span>}
          >
            {!selected ? (
              <p className="hint">Select an exhibit.</p>
            ) : selected.indexed === "full" ? (
              <p style={{ margin: 0, color: "var(--ink-2)" }}>
                Fully indexed: all {formatDuration(selected.duration_s)} is searchable, and every
                minute of it is disclosable. Nothing about this exhibit is hidden from a query.
              </p>
            ) : (
              <>
                <p style={{ margin: "0 0 10px", color: "var(--ink-2)" }}>
                  Registered, hashed and playable, but not transcribed.{" "}
                  {formatDuration(selected.dark_s)} of audio is dark: a search will never return it
                  until someone promotes it. That is a deliberate cost decision, not a failure.
                </p>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, 1fr)",
                    gap: 18,
                    marginTop: 4,
                  }}
                >
                  <div>
                    <div className="strip__k">Length</div>
                    <div className="tnum" style={{ fontSize: 15 }}>
                      {formatDuration(selected.duration_s)}
                    </div>
                    <div className="hint">unindexed</div>
                  </div>
                  <div>
                    <div className="strip__k">Indexed</div>
                    <div className="tnum" style={{ fontSize: 15, color: "var(--green)" }}>
                      0:00
                    </div>
                    <div className="hint">searchable</div>
                  </div>
                  <div>
                    <div className="strip__k">To promote</div>
                    <div className="tnum" style={{ fontSize: 15 }}>
                      {formatDuration(selected.duration_s)}
                    </div>
                    <div className="hint">one click</div>
                  </div>
                </div>
              </>
            )}
            {selected && (
              <>
                <div className="divider" />
                <div style={{ display: "flex", gap: 22, flexWrap: "wrap" }}>
                  <div>
                    <div className="strip__k">SHA-256</div>
                    <div className="mono" style={{ fontSize: 10.5, color: "var(--ink-3)" }}>
                      {provenance?.sha256.slice(0, 24) ?? "…"}…
                    </div>
                  </div>
                  <div>
                    <div className="strip__k">Segments</div>
                    <div className="tnum">{selected.segment_count}</div>
                  </div>
                  <div>
                    <div className="strip__k">Cue candidates</div>
                    <div className="tnum">
                      {selected.cue_total} <span className="hint">· {selected.cue_open} open</span>
                    </div>
                  </div>
                </div>
              </>
            )}
          </Panel>

          <Panel
            title={selected ? "Exhibit custody" : "Custody"}
            right={<span className="hint">newest first</span>}
            flush
          >
            <CustodyList events={events} />
          </Panel>
        </div>
      </div>
    </div>
  );
}

function UploadPanel({
  caseId,
  onDone,
}: {
  caseId: string;
  onDone: (recordingId?: string) => Promise<void> | void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [sidecar, setSidecar] = useState<File | null>(null);
  const [sourceType, setSourceType] = useState("interview");
  const [recordedAt, setRecordedAt] = useState("");
  const [mode, setMode] = useState<"full" | "triage">("full");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement | null>(null);

  const submit = useCallback(async () => {
    if (!file) {
      setError("Choose an audio file first.");
      return;
    }
    setBusy(true);
    setError(null);
    const form = new FormData();
    form.append("file", file);
    form.append("source_type", sourceType);
    form.append("case_id", caseId);
    if (recordedAt) form.append("recorded_at", new Date(recordedAt).toISOString());
    if (sidecar && mode === "full") form.append("transcript_file", sidecar);
    try {
      const created = mode === "triage" ? await api.uploadForTriage(form) : await api.upload(form);
      await onDone(created.status === "ready" ? created.id : undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }, [file, sidecar, sourceType, recordedAt, mode, onDone]);

  return (
    <Panel title="Add a recording" right={<span className="hint">original is hashed and read-only</span>}>
      <div className="upload" style={{ padding: 0 }}>
        <div
          className={`dropzone ${dragging ? "is-drag" : ""} ${file ? "is-filled" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const dropped = event.dataTransfer.files?.[0];
            if (dropped) setFile(dropped);
          }}
          onClick={() => input.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") input.current?.click();
          }}
        >
          <input
            ref={input}
            type="file"
            accept="audio/*,video/mp4"
            hidden
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <div className="dropzone__title">
            {file ? file.name : "Drop an audio file, or click to browse"}
          </div>
          <div className="dropzone__hint">
            {file
              ? `${formatBytes(file.size)} · ready to ingest`
              : "WAV, MP3, M4A, FLAC, OGG, AIFF, WMA, AMR · up to 512 MB"}
          </div>
        </div>

        <div className="upload__fields">
          <label className="field">
            <span>Intake</span>
            <select value={mode} onChange={(event) => setMode(event.target.value as "full" | "triage")}>
              <option value="full">Full: transcribe and index now</option>
              <option value="triage">Triage: register only, index later</option>
            </select>
          </label>
          <label className="field">
            <span>Source type</span>
            <select value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
              {SOURCE_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Recording date</span>
            <input
              type="datetime-local"
              value={recordedAt}
              onChange={(event) => setRecordedAt(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Reference transcript (optional)</span>
            <input
              type="file"
              accept="application/json"
              disabled={mode === "triage"}
              onChange={(event) => setSidecar(event.target.files?.[0] ?? null)}
            />
          </label>
          <button className="btn btn--primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "Ingesting…" : mode === "triage" ? "Register for triage" : "Upload and process"}
          </button>
          {error && <Notice tone="error">{error}</Notice>}
          <p className="hint" style={{ margin: 0 }}>
            Assigned to case <span className="mono">{caseId.slice(-6)}</span> on upload.
          </p>
        </div>
      </div>
    </Panel>
  );
}
