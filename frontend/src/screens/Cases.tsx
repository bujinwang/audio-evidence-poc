import { useMemo, useState } from "react";
import { Stamp } from "../components/Stamp";
import { MetaStrip, Panel, Empty, Bar } from "../components/Layout";
import { daysLabel, formatHours } from "../lib/format";
import type { CaseFile } from "../types";

type Filter = "all" | "due" | "review" | "cues";

export default function Cases({
  cases,
  onOpen,
  onRefresh,
}: {
  cases: CaseFile[];
  onOpen: (caseId: string, view?: "evidence") => void;
  onRefresh: () => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");

  const visible = useMemo(() => {
    return cases.filter((entry) => {
      if (filter === "due") return entry.days_to_disclosure !== null && entry.days_to_disclosure <= 7;
      if (filter === "review") return entry.totals.reviewed_pct < 100;
      if (filter === "cues") return entry.totals.cue_open > 0;
      return true;
    });
  }, [cases, filter]);

  const totals = useMemo(
    () => ({
      exhibits: cases.reduce((sum, entry) => sum + entry.totals.exhibits, 0),
      hours: cases.reduce((sum, entry) => sum + entry.totals.audio_hours, 0),
      atRisk: cases.filter(
        (entry) => entry.days_to_disclosure !== null && entry.days_to_disclosure <= 3,
      ).length,
      cues: cases.reduce((sum, entry) => sum + entry.totals.cue_open, 0),
    }),
    [cases],
  );

  return (
    <div className="view">
      <div className="pagehead">
        <div>
          <h1>Case queue</h1>
          <p>
            Every case carries its own exhibit list, custody record and disclosure clock. Sorted by
            what is due next, not by what was opened last.
          </p>
        </div>
        <div className="pagehead__right">
          {(
            [
              ["all", "All"],
              ["due", "Due this week"],
              ["review", "In review"],
              ["cues", "Cue sheet open"],
            ] as [Filter, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              className={`chip ${filter === value ? "is-on" : ""}`}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
          <button className="btn btn--sm" onClick={onRefresh}>
            Refresh
          </button>
        </div>
      </div>

      <MetaStrip
        items={[
          { k: "Open cases", v: cases.length },
          { k: "Exhibits", v: totals.exhibits },
          { k: "Hours of audio", v: totals.hours.toFixed(1) },
          { k: "Disclosure at risk", v: totals.atRisk, tone: totals.atRisk ? "var(--red)" : undefined },
          { k: "Cue decisions open", v: totals.cues },
        ]}
      />

      <div className="grid">
        <div className="panel">
          {visible.length === 0 ? (
            <Empty>No cases match this filter.</Empty>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th style={{ width: 104 }}>Case</th>
                  <th>Subject</th>
                  <th className="num" style={{ width: 80 }}>
                    Exhibits
                  </th>
                  <th style={{ width: 128 }}>Stage</th>
                  <th style={{ width: 120 }}>Owner</th>
                  <th style={{ width: 130 }}>Disclosure</th>
                  <th style={{ width: 56 }} />
                </tr>
              </thead>
              <tbody>
                {visible.map((entry) => {
                  const due = entry.days_to_disclosure;
                  const overdue = due !== null && due < 0;
                  const atRisk = due !== null && due <= 3;
                  const verified = entry.totals.reviewed_pct >= 100;
                  return (
                    <tr
                      key={entry.id}
                      className="is-clickable"
                      onClick={() => onOpen(entry.id)}
                      tabIndex={0}
                      onKeyDown={(event) => event.key === "Enter" && onOpen(entry.id)}
                    >
                      <td>
                        <span className="caseid">{entry.number}</span>
                      </td>
                      <td>
                        <div className="subject">{entry.subject}</div>
                        <div className="sub">
                          {entry.totals.exhibits} exhibits · {formatHours(entry.totals.audio_hours)} ·
                          {" "}
                          {entry.totals.cue_open} cue decisions open
                        </div>
                      </td>
                      <td className="num tnum">{entry.totals.exhibits}</td>
                      <td>
                        {overdue || atRisk ? (
                          <Stamp tone="flagged">{overdue ? "Overdue" : "At risk"}</Stamp>
                        ) : verified ? (
                          <Stamp tone="verified">Verified</Stamp>
                        ) : entry.totals.cue_open > 0 ? (
                          <Stamp tone="corrected">Redaction</Stamp>
                        ) : (
                          <Stamp tone="unreviewed">Open</Stamp>
                        )}
                      </td>
                      <td>{entry.owner ?? "—"}</td>
                      <td>
                        <span style={{ color: atRisk ? "var(--red)" : undefined }}>
                          {daysLabel(due)}
                        </span>
                      </td>
                      <td className="rowhint">↵ open</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <Panel title="Disclosure clock" right={<span className="hint">Crown · Alberta</span>}>
            {cases.length === 0 && <p className="hint">No cases registered yet.</p>}
            {cases
              .slice()
              .sort((a, b) => (a.days_to_disclosure ?? 999) - (b.days_to_disclosure ?? 999))
              .slice(0, 3)
              .map((entry) => {
                const due = entry.days_to_disclosure;
                const reviewed = entry.totals.reviewed_pct;
                return (
                  <div key={entry.id} style={{ marginBottom: 12 }}>
                    <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                      <span className="mono" style={{ fontSize: 11, color: "var(--ink-3)" }}>
                        {entry.number}
                      </span>
                      <strong style={{ color: due !== null && due <= 3 ? "var(--red)" : undefined }}>
                        {daysLabel(due)}
                      </strong>
                      <span className="hint">— {reviewed}% verified</span>
                    </div>
                    <div style={{ margin: "8px 0 4px" }}>
                      <Bar
                        value={reviewed}
                        tone={reviewed >= 100 ? "green" : due !== null && due <= 3 ? "amber" : undefined}
                      />
                    </div>
                    <div className="hint">
                      {reviewed}% of segments verified · {entry.totals.cue_open} cue decisions open
                    </div>
                  </div>
                );
              })}
          </Panel>

          <Panel title="What needs a person" flush>
            <table className="tbl">
              <tbody>
                {cases.flatMap((entry) =>
                  entry.exhibits
                    .filter((exhibit) => exhibit.reviewed_pct < 100 || exhibit.indexed === "none")
                    .slice(0, 2)
                    .map((exhibit) => (
                      <tr key={exhibit.id}>
                        <td className="mono" style={{ width: 66, whiteSpace: "nowrap" }}>
                          E-{String(entry.exhibits.indexOf(exhibit) + 1).padStart(3, "0")}
                        </td>
                        <td>
                          <div style={{ fontSize: 12 }}>{exhibit.filename}</div>
                          <div className="hint">
                            {entry.number} ·{" "}
                            {exhibit.indexed === "none"
                              ? "not indexed — registered only"
                              : `${exhibit.reviewed_pct}% reviewed`}
                          </div>
                        </td>
                        <td className="num" style={{ width: 92 }}>
                          {exhibit.indexed === "none" ? (
                            <Stamp tone="unreviewed">Promote?</Stamp>
                          ) : (
                            <Stamp tone="corrected">Review</Stamp>
                          )}
                        </td>
                      </tr>
                    )),
                )}
                {cases.every((entry) =>
                  entry.exhibits.every((exhibit) => exhibit.reviewed_pct >= 100),
                ) && (
                  <tr>
                    <td colSpan={3}>
                      <span className="hint">Nothing waiting. Every exhibit is verified.</span>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </Panel>
        </div>
      </div>
    </div>
  );
}
