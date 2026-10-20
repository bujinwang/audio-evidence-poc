export type Role = "admin" | "reviewer" | "investigator" | "viewer";
export type ReviewStatus = "ai_generated" | "in_review" | "corrected" | "verified";
export type SearchMode = "keyword" | "semantic" | "hybrid";

export interface Job {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  progress: number;
  error: string | null;
  detail: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Recording {
  id: string;
  filename: string;
  source_type: string;
  sha256: string;
  size_bytes: number;
  duration_s: number | null;
  sample_rate: number | null;
  channels: number | null;
  status: "uploaded" | "processing" | "ready" | "failed" | "triaged";
  error: string | null;
  uploaded_by: string;
  recorded_at: string | null;
  created_at: string;
  has_original: boolean;
  has_denoised: boolean;
  variants: string[];
  segment_count: number;
  review_counts: Record<string, number>;
  warnings: string[];
  processing_meta: Record<string, any>;
  latest_job: Job | null;
}

export interface Segment {
  id: string;
  recording_id: string;
  transcript_id: string;
  variant: string;
  idx: number;
  start_s: number;
  end_s: number;
  text: string;
  ai_text: string;
  speaker: string | null;
  review_status: ReviewStatus;
  version: number;
  edited: boolean;
  verified_by: string | null;
  verified_at: string | null;
}

export interface Transcript {
  id: string;
  recording_id: string;
  variant: string;
  engine: string;
  model_version: string;
  language: string | null;
  is_reference_copy: boolean;
  settings: Record<string, any>;
  created_at: string;
  segment_count: number;
  segments: Segment[];
}

export interface SearchHit {
  segment_id: string;
  recording_id: string;
  recording_filename: string;
  source_type: string;
  recorded_at: string | null;
  transcript_id: string;
  start_s: number;
  end_s: number;
  text: string;
  snippet: string;
  speaker: string | null;
  review_status: ReviewStatus;
  score: number;
  keyword_score: number | null;
  semantic_score: number | null;
  matched_by: string[];
}

export interface SearchResponse {
  query: string;
  mode: string;
  took_ms: number;
  total: number;
  hits: SearchHit[];
  diagnostics: Record<string, any>;
}

export interface Revision {
  id: string;
  version: number;
  prev_text: string;
  new_text: string;
  actor: string;
  action: string;
  note: string | null;
  created_at: string;
}

export interface AuditEvent {
  id: string;
  ts: string;
  actor: string;
  role: string;
  action: string;
  entity_type: string;
  entity_id: string;
  recording_id: string | null;
  detail: Record<string, any>;
}

export interface SegmentHistory {
  segment_id: string;
  ai_text: string;
  current_text: string;
  review_status: ReviewStatus;
  revisions: Revision[];
  events: AuditEvent[];
}

export interface Capability {
  name: string;
  available: boolean;
  detail: string;
  kind: "core" | "optional";
}

export interface Health {
  status: string;
  database: string;
  vector_store: string;
  embedder: string;
  transcriber: string;
  denoiser: string;
  capabilities: Capability[];
}

export interface SearchOptions {
  recordings: { id: string; filename: string; source_type: string; recorded_at: string | null }[];
  source_types: string[];
  speakers: string[];
  review_statuses: ReviewStatus[];
  modes: SearchMode[];
  default_limit: number;
}

export interface ComparisonRow {
  idx: number;
  start_s: number;
  end_s: number;
  original_text: string;
  denoised_text: string;
  speaker: string | null;
  similarity: number | null;
}

export interface Comparison {
  recording_id: string;
  available: boolean;
  reason: string | null;
  original_engine: string | null;
  denoised_engine: string | null;
  denoised_is_reference_copy: boolean;
  rows: ComparisonRow[];
}

export interface ProvenanceStep {
  stage: string;
  label: string;
  status: string;
  actor: string | null;
  role: string | null;
  at: string | null;
  detail: Record<string, any>;
}

export interface Provenance {
  recording_id: string;
  filename: string;
  sha256: string;
  source_type: string;
  intake_mode: string;
  status: string;
  steps: ProvenanceStep[];
}
