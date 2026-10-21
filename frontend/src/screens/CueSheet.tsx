import { useCallback, useEffect, useMemo, useState } from "react";
import { api, download } from "../api";
import { Empty, MetaStrip, Notice } from "../components/Layout";
import { ConfidenceChip, Stamp } from "../components/Stamp";
import { formatTimecode } from "../lib/format";
import type { CaseFile, Cue, CueDecision } from "../types";

const FILTERS: { value: string; label: string }[] = [
  { value: "", label: "All" },
  { value: "open", label: "Open" },
  { value: "accept", label: "Accepted" },
  { value: "reject", label: "Rejected" },
  { value: "not_redactable", label: "Not redactable" },
];

export default function CueSheet({
  caseFile,
  onChanged,
}: {
  caseFile: CaseFile;
  onChanged: () => Promise<void> | void;
}) {
  const [cues, setCues] = useState<Cue[]>([]);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setCues(await api.cues(caseFile.id));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the cue sheet");
    }
  }, [caseFile.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(
    () => ({
      total: cues.length,
      accept: cues.filter((cue) => cue.decision === "accept").length,
      reject: cues.filter((cue) => cue.decision === "reject").length,
      not_redactable: cues.filter((cue) => cue.decision === "not_redactable").length,
      open: cues.filter((cue) => cue.decision === "open").length,
    }),
    [cues],
  );

  const visible = filter ? cues.filter((cue) => cue.decision === filter) : cues;

  const decide = async (cue: Cue, decision: CueDecision) => {
    // Guard before the write, not only on the re-render: `busy` is state, so two
    // clicks inside one tick both pass the check and post twice.
    if (busy) return;
    if (cue.decision === decision) return;
    setBusy(true);
    try {
      const updated = await api.decideCue(cue.id, decision);
      setCues((previous) => previous.map((item) => (item.id === updated.id ? updated : item)));
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not record the decision");
    } finally {
      setBusy(false);
    }
  };

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(cues, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${caseFile.number}-cue-sheet.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="view">
      <div className="pagehead">
        <div>
          <h1>Redaction cue sheet</h1>
          <p>
            The audio half of the redaction pipeline. Every candidate carries a reason code, a
            confidence and a decision that survives disclosure. The export drives the blur pass; it
            does not perform it.
          </p>
        </div>
        <div className="pagehead__right">
          <button className="btn" onClick={exportJson}>
            Export JSON
          </button>
          <button
            className="btn btn--primary"
            onClick={() =>
              void download(
                `/api/cases/${caseFile.id}/cues.csv`,
                `${caseFile.number}-cue-sheet.csv`,
              ).catch((err) => setError(err instanceof Error ? err.message : "Export failed"))
            }
          >
            Export CSV
          </button>
        </div>
      </div>

      <MetaStrip
        items={[
          { k: "Candidates", v: counts.total },
          { k: "Accepted", v: counts.accept, tone: "var(--green)" },
          { k: "Rejected", v: counts.reject },
          { k: "Not redactable", v: counts.not_redactable },
          { k: "Open", v: counts.open, tone: counts.open ? "var(--amber)" : undefined },
          {
            k: "Exhibits covered",
            v: new Set(cues.map((cue) => cue.recording_id)).size,
          },
        ]}
      />

      {error && (
        <div className="grid">
          <Notice tone="error">{error}</Notice>
        </div>
      )}

      <div className="grid">
        <div className="panel">
          <div className="panel__head">
            <h2>Candidates</h2>
            <span className="right">
              {FILTERS.map((option) => (
                <button
                  key={option.value}
                  className={`chip ${filter === option.value ? "is-on" : ""}`}
                  onClick={() => setFilter(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </span>
          </div>

          {visible.length === 0 ? (
            <Empty>
              No candidates. The detector is rule-based: it finds phones, addresses, dates, plates
              and named categories (minors, medical, names).
            </Empty>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th style={{ width: 72 }}>Start</th>
                  <th style={{ width: 72 }}>End</th>
                  <th style={{ width: 64 }}>Ex.</th>
                  <th>Span</th>
                  <th style={{ width: 96 }}>Entity</th>
                  <th style={{ width: 96 }}>Reason</th>
                  <th className="num" style={{ width: 52 }}>
                    Conf
                  </th>
                  <th style={{ width: 200 }}>Decision</th>
                  <th style={{ width: 104 }}>Reviewer</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((cue) => (
                  <tr key={cue.id}>
                    <td className="mono">{formatTimecode(cue.start_s)}</td>
                    <td className="mono">{formatTimecode(cue.end_s)}</td>
                    <td className="mono" style={{ whiteSpace: "nowrap" }}>{cue.exhibit ?? "—"}</td>
                    <td>“{cue.span_text}”</td>
                    <td>{cue.entity_type}</td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {cue.reason_code}
                    </td>
                    <td className="num">
                      <ConfidenceChip value={cue.confidence} />
                    </td>
                    <td>
                      {cue.decision === "open" ? (
                        <div style={{ display: "flex", gap: 4 }}>
                          <button
                            className="btn btn--sm"
                            disabled={busy}
                            onClick={() => void decide(cue, "accept")}
                          >
                            Accept
                          </button>
                          <button
                            className="btn btn--sm"
                            disabled={busy}
                            onClick={() => void decide(cue, "reject")}
                          >
                            Reject
                          </button>
                          <button
                            className="btn btn--sm btn--quiet"
                            disabled={busy}
                            onClick={() => void decide(cue, "not_redactable")}
                          >
                            Not redactable
                          </button>
                        </div>
                      ) : (
                        <Stamp
                          tone={
                            cue.decision === "accept"
                              ? "verified"
                              : cue.decision === "reject"
                                ? "plain"
                                : "unreviewed"
                          }
                        >
                          {cue.decision.replace("_", " ")}
                        </Stamp>
                      )}
                    </td>
                    <td className="hint">{cue.reviewer ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div
            style={{
              padding: "10px 14px",
              borderTop: "1px solid var(--rule)",
              display: "flex",
              gap: 16,
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <span className="hint">Export schema</span>
            <code className="mono" style={{ fontSize: 11, color: "var(--ink-2)" }}>
              exhibit, filename, start_s, end_s, span_text, entity_type, reason_code, confidence,
              decision, reviewer, decided_at
            </code>
            <span className="hint" style={{ marginLeft: "auto" }}>
              EDL-compatible · one row per span
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
