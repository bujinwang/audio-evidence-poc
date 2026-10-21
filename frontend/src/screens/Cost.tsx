import { useEffect, useState } from "react";
import { api } from "../api";
import { Empty, MetaStrip, Notice, Panel } from "../components/Layout";
import { formatHours } from "../lib/format";
import type { CaseFile, CostModel, CostScenario } from "../types";

function money(value: number): string {
  if (value === 0) return "$0";
  if (value < 1) return `$${value.toFixed(2)}`;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function ScenarioTable({
  scenarios,
  hours,
  units,
  unitLabel,
}: {
  scenarios: CostScenario[];
  hours: number;
  units: number;
  unitLabel: string;
}) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th style={{ width: 200 }}>Approach</th>
          <th className="num" style={{ width: 96 }}>
            Compute
          </th>
          <th className="num" style={{ width: 88 }}>
            Cost
          </th>
          <th style={{ width: 150 }}>Index coverage</th>
          <th>Time to first answer</th>
        </tr>
      </thead>
      <tbody>
        {scenarios.map((scenario, index) => (
          <tr key={scenario.key} style={index === 2 ? { background: "var(--wash)" } : undefined}>
            <td>
              <b>{scenario.label}</b>
              <div className="sub">{scenario.detail}</div>
            </td>
            <td className="num mono">{scenario.compute_hours.toFixed(2)} h</td>
            <td className="num mono">{money(scenario.cost)}</td>
            <td>
              <div style={{ marginBottom: 3 }}>
                <div className="bar">
                  <i style={{ width: `${Math.max(2, scenario.coverage_pct)}%` }} />
                </div>
              </div>
              <span className="hint">{scenario.coverage_pct}%</span>
            </td>
            <td className="hint">{scenario.time_to_answer}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={5} className="hint" style={{ borderTop: "1px solid var(--rule-2)" }}>
            {hours.toLocaleString(undefined, { maximumFractionDigits: 2 })} hours of audio across{" "}
            {units.toLocaleString()} {unitLabel}
            {units === 1 ? "" : "s"}. Windowed promotion indexes 4 minutes per recording, not the
            whole file.
          </td>
        </tr>
      </tfoot>
    </table>
  );
}

export default function Cost({ caseFile }: { caseFile: CaseFile }) {
  const [model, setModel] = useState<CostModel | null>(null);
  const [archiveHours, setArchiveHours] = useState(2900);
  const [tapes, setTapes] = useState(3900);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .cost(caseFile.id, { archive_hours: archiveHours, tapes })
      .then(setModel)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load the cost model"));
  }, [caseFile.id, archiveHours, tapes]);

  if (error) {
    return (
      <div className="view">
        <div className="grid">
          <Notice tone="error">{error}</Notice>
        </div>
      </div>
    );
  }

  if (!model) {
    return (
      <div className="view">
        <Empty>Loading cost model…</Empty>
      </div>
    );
  }

  return (
    <div className="view">
      <div className="pagehead">
        <div>
          <h1>Cost model</h1>
          <p>
            Searching an archive is a purchase, not a default. This page prices three ways to answer
            the same question, so the customer can choose one.
          </p>
        </div>
        <div className="pagehead__right">
          <label className="hint" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            Archive hours
            <input
              className="input"
              style={{ width: 84 }}
              type="number"
              min={1}
              value={archiveHours}
              onChange={(event) => setArchiveHours(Number(event.target.value) || 1)}
            />
          </label>
          <label className="hint" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            Tapes
            <input
              className="input"
              style={{ width: 84 }}
              type="number"
              min={1}
              value={tapes}
              onChange={(event) => setTapes(Number(event.target.value) || 1)}
            />
          </label>
        </div>
      </div>

      <MetaStrip
        items={[
          { k: "This case", v: formatHours(model.audio_hours) },
          { k: "Exhibits", v: model.exhibits },
          { k: "Rate", v: `$${model.rate_per_hour.toFixed(2)} / h` },
          { k: "Measured RTF", v: model.rtf },
          { k: "Archive", v: `${model.archive_hours.toLocaleString()} h · ${model.archive_tapes.toLocaleString()} tapes` },
        ]}
      />

      <div className="grid">
        <Panel
          title="This case"
          right={<span className="hint">priced from the exhibits actually in the file</span>}
          flush
        >
          <ScenarioTable
            scenarios={model.scenarios}
            hours={model.audio_hours}
            units={model.exhibits}
            unitLabel="exhibit"
          />
        </Panel>

        <Panel
          title="If this were the archive"
          right={<span className="hint">list-price estimate · not a quote</span>}
          flush
        >
          <ScenarioTable
            scenarios={model.archive_scenarios}
            hours={model.archive_hours}
            units={model.archive_tapes}
            unitLabel="recording"
          />
        </Panel>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
          <Panel title="What this buys">
            <p style={{ margin: "0 0 10px", color: "var(--ink-2)" }}>
              At 100 hours of new audio a day, a full index costs about{" "}
              <strong>{money(model.rate_per_hour * 100)} a day</strong>{" "}
              and buys nothing until someone searches it. Windowed promotion answers a reopened case
              in four minutes.
            </p>
            <div className="divider" />
            {model.archive_scenarios.map((scenario) => (
              <div
                key={scenario.key}
                style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}
              >
                <span className="hint">Per audio hour — {scenario.label.toLowerCase()}</span>
                <span className="mono">
                  {money(scenario.cost / Math.max(model.archive_hours, 1))}
                </span>
              </div>
            ))}
          </Panel>

          <Panel title="Where it breaks">
            <ul style={{ margin: 0, paddingLeft: 16, color: "var(--ink-2)", fontSize: 12, lineHeight: 1.7 }}>
              <li>Spotting is a prototype: recall measured on a small set, not the whole archive.</li>
              <li>Overlapping speech still defeats the windowing heuristic.</li>
              <li>Above 1M segments, SQLite FTS gives way to Postgres; p95 was not measured.</li>
              <li>Denoising is skipped on triage — it costs more than transcription.</li>
            </ul>
            <div className="divider" />
            <div className="strip__k" style={{ marginBottom: 4 }}>
              Assumptions
            </div>
            <ul style={{ margin: 0, paddingLeft: 16, color: "var(--ink-3)", fontSize: 11, lineHeight: 1.6 }}>
              {model.assumptions.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}
