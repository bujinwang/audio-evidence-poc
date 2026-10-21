import type { BundlePlan, CaseFile } from "../types";
import { formatDate } from "../lib/format";

/**
 * The cover sheet as it will appear in the bundle.
 *
 * Rendered in the UI from the same numbers the PDF is built from, so the preview
 * cannot drift from the artifact. The warning line is deliberate: a package that
 * states its own review state is harder to attack than one that looks complete.
 */
export default function CoverSheet({
  caseFile,
  plan,
  reviewer,
}: {
  caseFile: CaseFile;
  plan: BundlePlan | null;
  reviewer: string;
}) {
  const exhibits = caseFile.exhibits;
  const unverified = plan?.unverified_exhibits ?? [];

  return (
    <div className="cover">
      <div className="cover__org">Technology North · Evidence Services</div>
      <h3>Disclosure package</h3>
      <table className="cover__tbl">
        <tbody>
          <tr>
            <td>Case</td>
            <td className="mono">{caseFile.number}</td>
          </tr>
          <tr>
            <td>Subject</td>
            <td>{caseFile.subject}</td>
          </tr>
          <tr>
            <td>Exhibits</td>
            <td>
              {exhibits.length === 0
                ? "—"
                : `E-001 – E-${String(exhibits.length).padStart(3, "0")} (${exhibits.length})`}
            </td>
          </tr>
          <tr>
            <td>Prepared by</td>
            <td>{reviewer}</td>
          </tr>
          <tr>
            <td>Prepared</td>
            <td>{formatDate(new Date().toISOString())}</td>
          </tr>
          <tr>
            <td>Review state</td>
            <td>
              {caseFile.totals.reviewed_pct}% reviewed
              {unverified.length > 0 ? ` · ${unverified.length} exhibits unverified` : ""}
            </td>
          </tr>
          <tr>
            <td>Manifest</td>
            <td className="mono">
              {plan?.manifest_sha256 ? `sha256:${plan.manifest_sha256.slice(0, 16)}…` : "computed on build"}
            </td>
          </tr>
        </tbody>
      </table>
      <p className="cover__note">
        Machine transcripts are identified per segment and are not evidence of truth. Corrections
        and verifications are recorded with reviewer identity and time. Redactions are listed in
        cue_sheet.csv with reason codes; no audio in this package has been altered. Verify the
        contents with <span className="mono">sha256sum -c manifest.sha256</span>.
      </p>
    </div>
  );
}
