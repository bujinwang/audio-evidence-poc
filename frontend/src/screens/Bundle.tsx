import { useEffect, useMemo, useState } from "react";
import { api, download } from "../api";
import { Notice, Panel } from "../components/Layout";
import { Stamp } from "../components/Stamp";
import CoverSheet from "../components/CoverSheet";
import { formatBytes } from "../lib/format";
import type { BundlePlan, CaseFile } from "../types";

const KIND_LABEL: Record<string, string> = {
  audio: "Audio clips",
  transcripts: "Transcripts",
  custody: "Custody records",
  cue: "Redaction cue sheet",
  cover: "Cover sheet",
  manifest: "Manifest",
};

export default function Bundle({ caseFile, actor }: { caseFile: CaseFile; actor: string }) {
  const [plan, setPlan] = useState<BundlePlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    api
      .bundlePlan(caseFile.id)
      .then(setPlan)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not plan the bundle"));
  }, [caseFile.id]);

  const grouped = useMemo(() => {
    const groups = new Map<string, { count: number; bytes: number }>();
    for (const item of plan?.items ?? []) {
      const current = groups.get(item.kind) ?? { count: 0, bytes: 0 };
      groups.set(item.kind, { count: current.count + 1, bytes: current.bytes + item.bytes });
    }
    return [...groups.entries()];
  }, [plan]);

  const build = async () => {
    setBusy(true);
    setError(null);
    try {
      await download(
        `/api/cases/${caseFile.id}/bundle.zip`,
        `${caseFile.number}-disclosure.zip`,
      );
      setNotice("Bundle built and downloaded. Verify it with sha256sum -c manifest.sha256.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not build the bundle");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="view">
      <div className="pagehead">
        <div>
          <h1>Disclosure bundle</h1>
          <p>
            What goes to the Crown, in one package with a manifest you can defend. Clips are copied
            from the original bytes, never re-encoded from a derivative.
          </p>
        </div>
        <div className="pagehead__right">
          <button
            className="btn"
            onClick={() =>
              void download(
                `/api/cases/${caseFile.id}/cues.csv`,
                `${caseFile.number}-cue-sheet.csv`,
              ).catch((err) => setError(err instanceof Error ? err.message : "Export failed"))
            }
          >
            Cue sheet only
          </button>
          <button className="btn btn--primary" onClick={() => void build()} disabled={busy}>
            {busy ? "Building…" : `Build bundle (${formatBytes(plan?.total_bytes ?? 0)})`}
          </button>
        </div>
      </div>

      {error && (
        <div className="grid">
          <Notice tone="error">{error}</Notice>
        </div>
      )}
      {notice && (
        <div className="grid">
          <Notice tone="info">{notice}</Notice>
        </div>
      )}

      <div className="grid" style={{ gridTemplateColumns: "1.05fr 1.25fr .95fr" }}>
        <Panel
          title="Contents"
          right={
            <span className="hint">
              {plan?.items.length ?? 0} files · {formatBytes(plan?.total_bytes ?? 0)}
            </span>
          }
          flush
        >
          <table className="tbl">
            <tbody>
              {grouped.map(([kind, group]) => (
                <tr key={kind}>
                  <td style={{ width: 20 }}>☑</td>
                  <td>
                    {KIND_LABEL[kind] ?? kind}
                    <div className="hint">
                      {group.count} file{group.count === 1 ? "" : "s"}
                    </div>
                  </td>
                  <td className="num mono" style={{ whiteSpace: "nowrap" }}>
                    {formatBytes(group.bytes)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ padding: "12px 14px", borderTop: "1px solid var(--rule)" }}>
            {plan && plan.warnings.length > 0 ? (
              <Notice tone="warn">
                <b>{plan.unverified_exhibits.length} exhibits unverified.</b>{" "}
                {plan.unverified_exhibits.join(", ")} — the bundle can be built, but the cover sheet
                records the state at preparation.
              </Notice>
            ) : (
              <Notice tone="info">Every exhibit is verified. Nothing to caveat on the cover sheet.</Notice>
            )}
          </div>
        </Panel>

        <Panel title="Cover sheet" right={<span className="hint">generated as PDF on build</span>}>
          <div style={{ background: "var(--surface-2)", margin: -14, padding: 14 }}>
            <CoverSheet caseFile={caseFile} plan={plan} reviewer={actor} />
          </div>
        </Panel>

        <Panel title="Manifest" right={<span className="hint">sha256</span>} flush>
          <div className="manifest mono">
            {(plan?.items ?? []).slice(0, 12).map((item) => (
              <div key={item.name}>
                <span title={item.name}>{item.name}</span>
                <b>{formatBytes(item.bytes)}</b>
              </div>
            ))}
            {(plan?.items.length ?? 0) > 12 && (
              <div>
                <span className="hint">+ {(plan?.items.length ?? 0) - 12} more</span>
                <b />
              </div>
            )}
          </div>
          <div style={{ padding: "12px 14px", borderTop: "1px solid var(--rule)" }}>
            <button className="btn btn--primary btn--block" onClick={() => void build()} disabled={busy}>
              {busy ? "Building…" : "Build bundle"}
            </button>
            <p className="hint" style={{ margin: "8px 0 0" }}>
              Signed with the case key. Verifiable offline with{" "}
              <span className="mono">sha256sum -c manifest.sha256</span>.
            </p>
            <div style={{ marginTop: 10 }}>
              <Stamp tone="plain">no audio is altered in this package</Stamp>
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}
