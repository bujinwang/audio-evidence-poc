import type {
  BundlePlan,
  CaseFile,
  Comparison,
  CostModel,
  Cue,
  CueDecision,
  Health,
  Provenance,
  Recording,
  Role,
  SearchMode,
  SearchOptions,
  SearchResponse,
  Segment,
  SegmentHistory,
  Transcript,
} from "./types";

/** Demo identity. A real deployment swaps this for an OIDC session. */
export const auth = {
  actor: localStorage.getItem("ae.actor") || "demo.investigator",
  role: (localStorage.getItem("ae.role") as Role) || "admin",
};

export function setAuth(actor: string, role: Role) {
  auth.actor = actor;
  auth.role = role;
  localStorage.setItem("ae.actor", actor);
  localStorage.setItem("ae.role", role);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Actor", auth.actor);
  headers.set("X-Role", auth.role);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === "string") detail = payload.detail;
      else if (Array.isArray(payload?.detail)) detail = payload.detail.map((d: any) => d.msg).join("; ");
    } catch {
      /* keep the status text */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => request<Health>("/api/health"),
  me: () => request<{ actor: string; role: string; permissions: string[] }>("/api/me"),
  config: () => request<Record<string, any>>("/api/config"),

  // --- cases ---------------------------------------------------------------
  cases: () => request<CaseFile[]>("/api/cases"),
  case: (caseId: string) => request<CaseFile>(`/api/cases/${caseId}`),
  cost: (caseId: string, params: { archive_hours?: number; tapes?: number } = {}) =>
    request<CostModel>(`/api/cases/${caseId}/cost${query(params)}`),
  bundlePlan: (caseId: string) => request<BundlePlan>(`/api/cases/${caseId}/bundle`),

  cues: (caseId: string, params: { decision?: string; recording_id?: string } = {}) =>
    request<Cue[]>(`/api/cases/${caseId}/cues${query(params)}`),
  decideCue: (cueId: string, decision: CueDecision) =>
    request<Cue>(`/api/cues/${cueId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  rescanCues: (recordingId: string) =>
    request<{ created: number; total: number }>(
      `/api/recordings/${recordingId}/cues/rescan`,
      { method: "POST" },
    ),

  listRecordings: () => request<Recording[]>("/api/recordings"),
  getRecording: (id: string) => request<Recording>(`/api/recordings/${id}`),
  retry: (id: string) => request<Recording>(`/api/recordings/${id}/retry`, { method: "POST" }),

  upload: (form: FormData) =>
    request<Recording>("/api/recordings", { method: "POST", body: form }),

  uploadForTriage: (form: FormData) =>
    request<Recording>("/api/recordings/triage", { method: "POST", body: form }),

  denoise: (id: string) =>
    request<Recording>(`/api/recordings/${id}/denoise`, { method: "POST" }),

  provenance: (id: string) => request<Provenance>(`/api/recordings/${id}/provenance`),

  transcript: (recordingId: string, variant = "original") =>
    request<Transcript>(`/api/recordings/${recordingId}/transcript?variant=${variant}`),
  comparison: (recordingId: string) =>
    request<Comparison>(`/api/recordings/${recordingId}/comparison`),

  search: (params: {
    q: string;
    mode: SearchMode;
    recording_id?: string[];
    source_type?: string[];
    review_status?: string[];
    limit?: number;
  }) => request<SearchResponse>(`/api/search${query(params as any)}`),
  searchOptions: () => request<SearchOptions>("/api/search/options"),

  updateSegment: (segmentId: string, text: string, note?: string) =>
    request<Segment>(`/api/segments/${segmentId}`, {
      method: "PATCH",
      body: JSON.stringify({ text, note: note ?? null }),
    }),
  verifySegment: (segmentId: string, verified: boolean, note?: string) =>
    request<Segment>(`/api/segments/${segmentId}/verify`, {
      method: "POST",
      body: JSON.stringify({ verified, note: note ?? null }),
    }),
  reopenSegment: (segmentId: string) =>
    request<Segment>(`/api/segments/${segmentId}/reopen`, { method: "POST" }),
  history: (segmentId: string) => request<SegmentHistory>(`/api/segments/${segmentId}/history`),

  audioUrl: (recordingId: string, variant = "original") =>
    `/api/recordings/${recordingId}/audio?variant=${variant}`,
};

/**
 * Download a response as a file, carrying the acting identity.
 *
 * A plain <a href> would drop the X-Actor/X-Role headers and, in a deployment
 * with anonymous access off, hand the user a 401 instead of a disclosure bundle.
 */
export async function download(path: string, fallbackName: string): Promise<void> {
  const headers = new Headers({ "X-Actor": auth.actor, "X-Role": auth.role });
  const response = await fetch(path, { headers });
  if (!response.ok) throw new ApiError(response.status, `Download failed (${response.status})`);
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const name = match?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export const audioUrl = api.audioUrl;
