import { useCallback, useEffect, useMemo, useState } from "react";
import { api, auth, setAuth } from "./api";
import { daysLabel } from "./lib/format";
import Cases from "./screens/Cases";
import Evidence from "./screens/Evidence";
import Find from "./screens/Find";
import Review from "./screens/Review";
import CueSheet from "./screens/CueSheet";
import Bundle from "./screens/Bundle";
import Cost from "./screens/Cost";
import type { CaseFile, Health, Role } from "./types";

type View = "cases" | "evidence" | "find" | "review" | "cues" | "bundle" | "cost";

interface Route {
  view: View;
  caseId: string | null;
  recordingId: string | null;
}

const ROLES: { value: Role; label: string }[] = [
  { value: "admin", label: "Administrator" },
  { value: "reviewer", label: "Reviewer" },
  { value: "investigator", label: "Investigator" },
  { value: "viewer", label: "Read-only viewer" },
];

const VIEWS: View[] = ["cases", "evidence", "find", "review", "cues", "bundle", "cost"];

/** `#/case/<id>/<view>[/<recordingId>]` — deep-linkable, no router dependency. */
function parseHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  if (parts[0] === "case" && parts[1]) {
    const view = (parts[2] as View) ?? "evidence";
    return {
      view: VIEWS.includes(view) ? view : "evidence",
      caseId: parts[1],
      recordingId: parts[3] ?? null,
    };
  }
  return { view: "cases", caseId: null, recordingId: null };
}

function writeHash(route: Route) {
  const next =
    route.view === "cases" || !route.caseId
      ? "#/cases"
      : `#/case/${route.caseId}/${route.view}${
          route.recordingId ? `/${route.recordingId}` : ""
        }`;
  if (window.location.hash !== next) window.location.hash = next;
}

export default function App() {
  const initial = useMemo(parseHash, []);
  const [route, setRoute] = useState<Route>(initial);
  const [cases, setCases] = useState<CaseFile[]>([]);
  const [caseFile, setCaseFile] = useState<CaseFile | null>(null);
  const [focusSegment, setFocusSegment] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actor, setActor] = useState(auth.actor);
  const [role, setRole] = useState<Role>(auth.role);

  const navigate = useCallback((next: Route) => {
    setRoute(next);
    writeHash(next);
  }, []);

  // The rail is a set of destinations, not a set of conditionals. If no case is
  // open, act on the top of the queue — the case the operator is most likely to
  // mean — instead of presenting a dead button.
  const openCaseFile = caseFile && caseFile.id === route.caseId ? caseFile : null;
  const activeCase = openCaseFile ?? cases[0] ?? null;
  const go = (view: View) =>
    activeCase && navigate({ view, caseId: activeCase.id, recordingId: null });

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const loadCases = useCallback(async () => {
    try {
      setCases(await api.cases());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load cases");
    }
  }, []);

  const loadCase = useCallback(async (caseId: string) => {
    try {
      setCaseFile(await api.case(caseId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the case");
    }
  }, []);

  useEffect(() => {
    void loadCases();
    api.health().then(setHealth).catch(() => setHealth(null));
  }, [loadCases]);

  useEffect(() => {
    if (route.caseId) void loadCase(route.caseId);
    else setCaseFile(null);
  }, [route.caseId, loadCase]);

  const refreshCase = useCallback(async () => {
    await loadCases();
    if (route.caseId) await loadCase(route.caseId);
  }, [loadCases, loadCase, route.caseId]);

  // "/" jumps to Find from anywhere; Escape returns to the queue.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (event.key === "/" && !typing) {
        event.preventDefault();
        const caseId = route.caseId ?? activeCase?.id;
        if (caseId) navigate({ view: "find", caseId, recordingId: null });
      }
      if (event.key === "Escape" && !typing) navigate({ view: "cases", caseId: null, recordingId: null });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, route, activeCase]);

  const applyRole = (nextRole: Role) => {
    setRole(nextRole);
    setAuth(actor, nextRole);
  };
  const applyActor = (nextActor: string) => {
    setActor(nextActor);
    setAuth(nextActor, role);
  };

  const openCase = (caseId: string, view: View = "evidence") =>
    navigate({ view, caseId, recordingId: null });

  const degraded = health?.capabilities.filter((capability) => !capability.available) ?? [];
  const due = caseFile?.days_to_disclosure;
  const atRisk = due !== null && due !== undefined && due <= 3;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand__mark">ER</span>
          <div>
            <div className="brand__name">Evidence Review</div>
            <div className="brand__sub">Technology North</div>
          </div>
        </div>

        <div className="crumb">
          {activeCase ? (
            <>
              <span className="crumb__case">{activeCase.subject}</span>
              <span className="sep">/</span>
              <strong className="mono">{activeCase.number}</strong>
            </>
          ) : (
            <span className="crumb__case">Case queue</span>
          )}
        </div>

        <div className="topbar__right">
          {caseFile && (
            <div className={`clock ${atRisk ? "is-risk" : ""}`}>
              <span>Crown disclosure</span>
              <b>{daysLabel(caseFile.days_to_disclosure)}</b>
              {caseFile.totals.reviewed_pct < 100 && (
                <span className="hint">· {caseFile.totals.reviewed_pct}% reviewed</span>
              )}
            </div>
          )}
          <div className="actor">
            <span
              className="actor__dot"
              style={{ background: degraded.length ? "var(--amber)" : "var(--green)" }}
              title={
                health
                  ? `database ${health.database} · transcriber ${health.transcriber}`
                  : "status unavailable"
              }
            />
            <input
              className="actor__name"
              style={{ border: 0, background: "transparent", width: 132 }}
              value={actor}
              onChange={(event) => applyActor(event.target.value)}
              aria-label="Acting user"
            />
            <select
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
        </div>
      </header>

      <nav className="rail" aria-label="Workspace">
        <div className="rail__group">Case</div>
        <button
          className={`rail__item ${route.view === "cases" ? "is-active" : ""}`}
          onClick={() => navigate({ view: "cases", caseId: null, recordingId: null })}
        >
          <svg viewBox="0 0 16 16">
            <path d="M2 4.5h4l1.2 1.5H14v7.5H2z" />
          </svg>
          <span>Case queue</span>
          <span className="rail__count">{cases.length}</span>
        </button>
        <button
          className={`rail__item ${route.view === "evidence" ? "is-active" : ""}`}
          disabled={!activeCase}
          onClick={() => go("evidence")}
        >
          <svg viewBox="0 0 16 16">
            <path d="M2.5 3.5h11v9h-11z" />
            <path d="M2.5 6.5h11" />
          </svg>
          <span>Evidence</span>
          <span className="rail__count">{activeCase?.exhibits.length ?? "—"}</span>
        </button>
        <button
          className={`rail__item ${route.view === "find" ? "is-active" : ""}`}
          disabled={!activeCase}
          onClick={() => go("find")}
        >
          <svg viewBox="0 0 16 16">
            <circle cx="7" cy="7" r="3.6" />
            <path d="M10 10l3.5 3.5" />
          </svg>
          <span>Find</span>
        </button>
        <button
          className={`rail__item ${route.view === "review" ? "is-active" : ""}`}
          disabled={!activeCase}
          onClick={() => go("review")}
        >
          <svg viewBox="0 0 16 16">
            <path d="M2 8h1.5l1-3 1.6 6 1.4-8 1.4 9 1.2-4H14" />
          </svg>
          <span>Review</span>
        </button>

        <div className="rail__group">Disclosure</div>
        <button
          className={`rail__item ${route.view === "cues" ? "is-active" : ""}`}
          disabled={!activeCase}
          onClick={() => go("cues")}
        >
          <svg viewBox="0 0 16 16">
            <path d="M3 3h10v10H3z" />
            <path d="M6 3v10M10 3v10M3 8h10" />
          </svg>
          <span>Cue sheet</span>
          <span className="rail__count">{activeCase?.totals.cue_open ?? "—"}</span>
        </button>
        <button
          className={`rail__item ${route.view === "bundle" ? "is-active" : ""}`}
          disabled={!activeCase}
          onClick={() => go("bundle")}
        >
          <svg viewBox="0 0 16 16">
            <path d="M3 6.5l5-3 5 3v6l-5 2.5-5-2.5z" />
            <path d="M3 6.5l5 3 5-3M8 9.5V15" />
          </svg>
          <span>Bundle</span>
        </button>

        <div className="rail__group">Planning</div>
        <button
          className={`rail__item ${route.view === "cost" ? "is-active" : ""}`}
          disabled={!activeCase}
          onClick={() => go("cost")}
        >
          <svg viewBox="0 0 16 16">
            <path d="M3 13V7M8 13V3M13 13V9" />
          </svg>
          <span>Cost model</span>
        </button>

        <div className="rail__foot">
          Offline profile
          <br />
          No outbound network
          <br />
          <span className="mono">sha256 verified</span>
        </div>
      </nav>

      <main className="main">
        {error && (
          <div className="grid">
            <div className="notice notice--error">{error}</div>
          </div>
        )}

        {route.view === "cases" && (
          <Cases cases={cases} onOpen={openCase} onRefresh={loadCases} />
        )}

        {route.view !== "cases" && openCaseFile && (
          <>
            {route.view === "evidence" && (
              <Evidence
                key={openCaseFile.id}
                caseFile={openCaseFile}
                onOpenReview={(recordingId) =>
                  navigate({ view: "review", caseId: openCaseFile.id, recordingId })
                }
                onOpenBundle={() =>
                  navigate({ view: "bundle", caseId: openCaseFile.id, recordingId: null })
                }
                onChanged={refreshCase}
              />
            )}
            {route.view === "find" && (
              <Find
                key={openCaseFile.id}
                caseFile={openCaseFile}
                // The archive, for naming a hit that came from another case and
                // for answering "is this phrase in the archive?" on a case-scope
                // zero. `cases` is already loaded for the rail and the queue, so
                // this hands over data the app has rather than adding a request.
                cases={cases}
                onOpenReview={(recordingId, segmentId) => {
                  setFocusSegment(segmentId ?? null);
                  navigate({ view: "review", caseId: openCaseFile.id, recordingId });
                }}
              />
            )}
            {route.view === "review" && (
              <Review
                key={`${openCaseFile.id}:${route.recordingId ?? ""}`}
                caseFile={openCaseFile}
                recordingId={route.recordingId}
                focusSegmentId={focusSegment}
                onSelectRecording={(recordingId) =>
                  navigate({ view: "review", caseId: openCaseFile.id, recordingId })
                }
                onChanged={refreshCase}
              />
            )}
            {route.view === "cues" && <CueSheet key={openCaseFile.id} caseFile={openCaseFile} onChanged={refreshCase} />}
            {route.view === "bundle" && <Bundle key={openCaseFile.id} caseFile={openCaseFile} actor={actor} />}
            {route.view === "cost" && <Cost key={openCaseFile.id} caseFile={openCaseFile} />}
          </>
        )}

        {route.view !== "cases" && !openCaseFile && (
          <div className="empty">Loading case…</div>
        )}
      </main>
    </div>
  );
}
