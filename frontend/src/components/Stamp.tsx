import type { ReviewStatus } from "../types";

type Tone = "verified" | "corrected" | "unreviewed" | "flagged" | "info" | "plain";

export function Stamp({
  tone = "plain",
  children,
  title,
}: {
  tone?: Tone;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span className={`stamp stamp--${tone}`} title={title}>
      {children}
    </span>
  );
}

const REVIEW_TONE: Record<ReviewStatus, Tone> = {
  ai_generated: "unreviewed",
  in_review: "corrected",
  corrected: "corrected",
  verified: "verified",
};

const REVIEW_LABEL: Record<ReviewStatus, string> = {
  ai_generated: "Unreviewed",
  in_review: "In review",
  corrected: "Corrected",
  verified: "Verified",
};

export function ReviewStamp({ status }: { status: ReviewStatus }) {
  return <Stamp tone={REVIEW_TONE[status] ?? "plain"}>{REVIEW_LABEL[status] ?? status}</Stamp>;
}

/**
 * The line the PRD draws in §10: AI relevance and transcription are not proof of
 * fact. The badge is the same message rendered as a reusable, consistent mark
 * wherever the system shows an AI-generated match or passage, so the operator is
 * never left to infer the distinction. It is deliberately the `plain` (dashed,
 * muted) stamp — quiet, not alarming — because it accompanies every result and
 * must not read as an error. Used on the Find results summary and on every
 * semantic (similarity) hit, the match type most easily mistaken for a factual
 * finding.
 */
export function VeracityBadge({ label }: { label?: string }) {
  return (
    <span
      className="stamp stamp--plain"
      title="AI-generated match — not evidence of fact. Listen to the source before relying on it."
    >
      {label ?? "AI match · not evidence"}
    </span>
  );
}

/** 0–1 confidence as the reviewer sees it: a number and a colour, never colour alone. */
export function ConfidenceChip({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const tone = value >= 0.9 ? "high" : value >= 0.75 ? "mid" : "low";
  return (
    <span className={`conf conf--${tone}`} title={`${pct}% confidence`}>
      {pct}
    </span>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  interview: "Interview",
  bodycam: "Bodycam",
  cassette: "Cassette",
  phone_call: "Phone call",
  sports: "Sport",
  speech: "Speech",
  other: "Other",
};

export function SourceTag({ source }: { source: string }) {
  return (
    <span
      className="chip"
      style={{ height: 20, padding: "0 7px", fontSize: 10.5, whiteSpace: "nowrap" }}
    >
      {SOURCE_LABEL[source] ?? source}
    </span>
  );
}
