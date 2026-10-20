import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Notice, ProgressBar, ReviewBadge, SourceTag, StatusBadge } from "../components/Badges";
import { formatBytes, formatDate, formatTime } from "../lib/format";
import type { Health, Recording } from "../types";

interface LibraryProps {
  onOpen: (recordingId: string) => void;
  health: Health | null;
}

const SOURCE_TYPES = [
  { value: "interview", label: "Interview" },
  { value: "bodycam", label: "Body camera" },
  { value: "cassette", label: "Cassette transfer" },
  { value: "phone_call", label: "Phone call" },
  { value: "other", label: "Other" },
];

export default function Library({ onOpen, health }: LibraryProps) {
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [sourceType, setSourceType] = useState("interview");
  const [recordedAt, setRecordedAt] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sidecar, setSidecar] = useState<File | null>(null);
  const [intakeMode, setIntakeMode] = useState<"full" | "triage">("full");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const sidecarInput = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      const rows = await api.listRecordings();
      setRecordings(rows);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load recordings");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const busy = useMemo(
    () => recordings.some((r) => r.status === "processing" || r.status === "uploaded"),
    [recordings],
  );

  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void load(), 1500);
    return () => window.clearInterval(timer);
  }, [busy, load]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) {
      setError("Choose an audio file first.");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("source_type", sourceType);
    if (recordedAt) form.append("recorded_at", new Date(recordedAt).toISOString());
    if (sidecar && intakeMode === "full") form.append("transcript_file", sidecar);

    setUploading(true);
    setError(null);
    try {
      const created =
        intakeMode === "triage" ? await api.uploadForTriage(form) : await api.upload(form);
      setFile(null);
      setSidecar(null);
      setRecordedAt("");
      if (fileInput.current) fileInput.current.value = "";
      if (sidecarInput.current) sidecarInput.current.value = "";
      await load();
      if (created.status === "ready") onOpen(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const retry = async (id: string) => {
    try {
      await api.retry(id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Retry failed");
    }
  };

  const transcriptionCapability = health?.capabilities.find((c) => c.name === "transcription");
  const denoiseCapability = health?.capabilities.find((c) => c.name === "denoising");

  return (
    <div className="screen">
      <section className="panel">
        <header className="panel__head">
          <div>
            <h2>Add a recording</h2>
            <p className="muted">
              The original is stored unchanged, hashed, and made read-only. All processing
              happens on separate derivatives.
            </p>
          </div>
        </header>

        <form className="upload" onSubmit={submit}>
          <div
            className={`dropzone ${dragging ? "dropzone--active" : ""} ${file ? "dropzone--filled" : ""}`}
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
            onClick={() => fileInput.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") fileInput.current?.click();
            }}
          >
            <input
              ref={fileInput}
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
                : "WAV, MP3, M4A, FLAC, OGG, AIFF, WMA, AMR — up to 512 MB"}
            </div>
          </div>

          <div className="upload__fields">
            <label className="field">
              <span>Intake</span>
              <select
                value={intakeMode}
                onChange={(event) => setIntakeMode(event.target.value as "full" | "triage")}
              >
                <option value="full">Full — transcribe and index now</option>
                <option value="triage">Triage — register only, index later</option>
              </select>
              <small>
                Triage hashes, probes and tracks the file without paying for speech
                recognition or embeddings. Promote it when a case needs it searchable.
              </small>
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
                ref={sidecarInput}
                type="file"
                accept="application/json"
                disabled={intakeMode === "triage"}
                onChange={(event) => setSidecar(event.target.files?.[0] ?? null)}
              />
              <small>
                {intakeMode === "triage"
                  ? "Not used in triage mode — no transcription runs."
                  : "JSON with segments[]. Used when no ASR engine is installed, and for scoring accuracy later."}
              </small>
            </label>

            <button className="btn btn--primary upload__submit" type="submit" disabled={uploading}>
              {uploading
                ? "Ingesting…"
                : intakeMode === "triage"
                  ? "Register for triage"
                  : "Upload and process"}
            </button>
          </div>
        </form>

        {!transcriptionCapability?.available && (
          <Notice tone="warn">
            <strong>No speech-recognition engine detected.</strong>{" "}
            {transcriptionCapability?.detail ??
              "Install WhisperX or upload a reference transcript alongside the audio."}
          </Notice>
        )}
        {!denoiseCapability?.available && (
          <Notice tone="info">
            Denoising is unavailable in this environment ({denoiseCapability?.detail}). Recordings
            are still transcribed and searchable; the original remains the reference.
          </Notice>
        )}
        {error && <Notice tone="error">{error}</Notice>}
      </section>

      <section className="panel">
        <header className="panel__head">
          <div>
            <h2>Audio library</h2>
            <p className="muted">
              {recordings.length} recording{recordings.length === 1 ? "" : "s"}
              {busy ? " · processing in progress" : ""}
            </p>
          </div>
          <button className="btn btn--ghost" type="button" onClick={() => void load()}>
            Refresh
          </button>
        </header>

        {loading ? (
          <div className="empty">Loading…</div>
        ) : recordings.length === 0 ? (
          <div className="empty">
            Nothing here yet. Generate the demo dataset with{" "}
            <code>python scripts/make_demo_data.py --upload http://127.0.0.1:8000</code>.
          </div>
        ) : (
          <div className="table">
            <div className="table__head">
              <span>Recording</span>
              <span>Recorded</span>
              <span>Duration</span>
              <span>Processing</span>
              <span>Review</span>
              <span />
            </div>
            {recordings.map((recording) => {
              const job = recording.latest_job;
              const verified = recording.review_counts.verified ?? 0;
              const corrected = recording.review_counts.corrected ?? 0;
              return (
                <div className="table__row" key={recording.id}>
                  <div className="table__cell">
                    <div className="recording__name" title={recording.filename}>
                      {recording.filename}
                    </div>
                    <div className="recording__meta">
                      <SourceTag source={recording.source_type} />
                      <span className="mono dim">{recording.id}</span>
                    </div>
                    {recording.warnings.length > 0 && (
                      <div className="recording__warn">{recording.warnings[0]}</div>
                    )}
                    {recording.error && <div className="recording__error">{recording.error}</div>}
                  </div>

                  <div className="table__cell dim">{formatDate(recording.recorded_at)}</div>
                  <div className="table__cell mono">{formatTime(recording.duration_s)}</div>

                  <div className="table__cell">
                    <StatusBadge status={recording.status} />
                    {job && (job.status === "running" || job.status === "queued") && (
                      <div className="table__progress">
                        <ProgressBar value={job.progress} />
                        <span className="dim small">{job.stage}</span>
                      </div>
                    )}
                    {job?.status === "failed" && (
                      <div className="recording__error small">{job.error}</div>
                    )}
                  </div>

                  <div className="table__cell">
                    <div className="review-summary">
                      <span className="mono">{recording.segment_count}</span>
                      <span className="dim small">segments</span>
                    </div>
                    <div className="review-chips">
                      {verified > 0 && <ReviewBadge status="verified" />}
                      {corrected > 0 && <ReviewBadge status="corrected" />}
                      {verified > 0 && (
                        <span className="dim small">
                          {verified}/{recording.segment_count} verified
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="table__cell table__cell--actions">
                    {recording.status === "triaged" ? (
                      <button
                        className="btn btn--primary btn--small"
                        type="button"
                        onClick={() => void retry(recording.id)}
                        title="Run speech recognition and build the semantic index for this recording"
                      >
                        Index now
                      </button>
                    ) : (
                      <button
                        className="btn btn--small"
                        type="button"
                        onClick={() => onOpen(recording.id)}
                        disabled={recording.segment_count === 0}
                      >
                        Review
                      </button>
                    )}
                    {recording.status === "failed" && (
                      <button
                        className="btn btn--small btn--ghost"
                        type="button"
                        onClick={() => void retry(recording.id)}
                      >
                        Retry
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
