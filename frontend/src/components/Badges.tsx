import { reviewLabel } from "../lib/format";

export function ReviewBadge({ status }: { status: string }) {
  return <span className={`badge badge--${status}`}>{reviewLabel(status)}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const labels: Record<string, string> = {
    uploaded: "Queued",
    processing: "Processing",
    ready: "Ready",
    failed: "Failed",
    triaged: "Triaged",
  };
  return <span className={`badge badge--status-${status}`}>{labels[status] ?? status}</span>;
}

export function SourceTag({ source }: { source: string }) {
  const labels: Record<string, string> = {
    interview: "Interview",
    bodycam: "Bodycam",
    cassette: "Cassette",
    phone_call: "Phone call",
    other: "Other",
  };
  return <span className="tag">{labels[source] ?? source}</span>;
}

export function SignalTag({ signal }: { signal: string }) {
  return (
    <span className={`tag tag--${signal}`} title={`Matched by ${signal} retrieval`}>
      {signal}
    </span>
  );
}

export function ProgressBar({ value }: { value: number }) {
  return (
    <div className="progress" role="progressbar" aria-valuenow={Math.round(value * 100)}>
      <div className="progress__fill" style={{ width: `${Math.max(2, value * 100)}%` }} />
    </div>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "error";
  children: React.ReactNode;
}) {
  return <div className={`notice notice--${tone}`}>{children}</div>;
}
