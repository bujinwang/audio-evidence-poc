import { useCallback, useEffect, useState } from "react";
import { api, auth, setAuth } from "./api";
import Library from "./screens/Library";
import Review from "./screens/Review";
import Search from "./screens/Search";
import type { Health, Role } from "./types";

type View = "library" | "search" | "review";

interface Target {
  recordingId: string;
  segmentId?: string | null;
  start?: number | null;
}

const ROLES: { value: Role; label: string }[] = [
  { value: "admin", label: "Administrator" },
  { value: "reviewer", label: "Transcript reviewer" },
  { value: "investigator", label: "Investigator" },
  { value: "viewer", label: "Read-only viewer" },
];

/** Minimal hash routing so a screen (and a passage) can be linked directly. */
function parseHash(): { view: View; target: Target | null } {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, search] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(search ?? "");

  if (parts[0] === "library") return { view: "library", target: null };
  if (parts[0] === "review" && parts[1]) {
    const start = Number(params.get("t"));
    return {
      view: "review",
      target: {
        recordingId: parts[1],
        segmentId: params.get("segment"),
        start: Number.isFinite(start) && params.get("t") !== null ? start : null,
      },
    };
  }
  return { view: "search", target: null };
}

function writeHash(view: View, target: Target | null) {
  let next = "#/search";
  if (view === "library") next = "#/library";
  if (view === "review" && target) {
    const params = new URLSearchParams();
    if (target.segmentId) params.set("segment", target.segmentId);
    if (target.start !== null && target.start !== undefined) params.set("t", String(target.start));
    const suffix = params.toString();
    next = `#/review/${target.recordingId}${suffix ? `?${suffix}` : ""}`;
  }
  if (window.location.hash !== next) window.location.hash = next;
}

export default function App() {
  const initial = parseHash();
  const [view, setView] = useState<View>(initial.view);
  const [target, setTarget] = useState<Target | null>(initial.target);
  const [health, setHealth] = useState<Health | null>(null);
  const [actor, setActor] = useState(auth.actor);
  const [role, setRole] = useState<Role>(auth.role);

  useEffect(() => {
    const onHashChange = () => {
      const parsed = parseHash();
      setView(parsed.view);
      setTarget(parsed.target);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = (nextView: View, nextTarget: Target | null = null) => {
    setView(nextView);
    setTarget(nextTarget);
    writeHash(nextView, nextTarget);
  };

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      setHealth(null);
    }
  }, []);

  useEffect(() => {
    void loadHealth();
    const timer = window.setInterval(() => void loadHealth(), 30000);
    return () => window.clearInterval(timer);
  }, [loadHealth]);

  const applyRole = (nextRole: Role) => {
    setRole(nextRole);
    setAuth(actor, nextRole);
  };

  const applyActor = (nextActor: string) => {
    setActor(nextActor);
    setAuth(nextActor, role);
  };

  const openReview = (recordingId: string, segmentId?: string | null, start?: number | null) => {
    navigate("review", { recordingId, segmentId: segmentId ?? null, start: start ?? null });
  };

  const degraded = health?.capabilities.filter((capability) => !capability.available) ?? [];

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__mark">◉</span>
          <div>
            <div className="topbar__title">Audio Evidence Review</div>
            <div className="topbar__sub">Proof of concept · Technology North</div>
          </div>
        </div>

        <nav className="topbar__nav">
          <button
            type="button"
            className={`navtab ${view === "search" ? "is-active" : ""}`}
            onClick={() => navigate("search")}
          >
            Search
          </button>
          <button
            type="button"
            className={`navtab ${view === "library" ? "is-active" : ""}`}
            onClick={() => navigate("library")}
          >
            Library
          </button>
          {target && (
            <button
              type="button"
              className={`navtab ${view === "review" ? "is-active" : ""}`}
              onClick={() => navigate("review", target)}
            >
              Review
            </button>
          )}
        </nav>

        <div className="topbar__identity">
          <span
            className={`health-dot ${health?.status === "ok" ? "is-ok" : "is-degraded"}`}
            title={
              health
                ? `database ${health.database} · transcriber ${health.transcriber} · denoiser ${health.denoiser}`
                : "status unavailable"
            }
          />
          <input
            className="input input--tight"
            value={actor}
            onChange={(event) => applyActor(event.target.value)}
            aria-label="Acting user"
          />
          <select
            className="select select--tight"
            value={role}
            onChange={(event) => applyRole(event.target.value as Role)}
            aria-label="Acting role"
          >
            {ROLES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      {degraded.length > 0 && view !== "library" && (
        <div className="statusbar">
          {degraded.map((capability) => (
            <span key={capability.name} className="statusbar__item">
              <strong>{capability.name}</strong> unavailable — {capability.detail}
            </span>
          ))}
        </div>
      )}

      <main className="content">
        {view === "search" && (
          <Search key={`search-${role}`} onOpen={(recordingId, segmentId, start) => openReview(recordingId, segmentId, start)} />
        )}
        {view === "library" && (
          <Library key={`library-${role}`} health={health} onOpen={(recordingId) => openReview(recordingId)} />
        )}
        {view === "review" && target && (
          <Review
            key={`review-${target.recordingId}-${role}`}
            recordingId={target.recordingId}
            focusSegmentId={target.segmentId}
            focusStart={target.start}
            onBack={() => navigate("search")}
          />
        )}
        {view === "review" && !target && (
          <div className="empty">Pick a passage from search, or open a recording from the library.</div>
        )}
      </main>

      <footer className="footer">
        AI output is not evidence of truth. Transcripts are machine-generated until a reviewer
        verifies them, and verification is recorded per segment with reviewer identity and time.
      </footer>
    </div>
  );
}
