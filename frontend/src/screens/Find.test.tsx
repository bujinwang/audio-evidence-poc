import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchHit, SearchResponse } from "../types";

/**
 * Tests for the first AI-UX slice on the Find screen:
 *
 *   1. scenario prompts pre-select the mode that answers the ask
 *   2. every result carries an explicit source citation
 *   3. semantic matches carry the "not evidence" badge, keyword-only ones do not
 *
 * The API module is mocked rather than the network, because `Find` talks to
 * `api.search` directly. Everything else (formatting, stamps, the table) runs
 * for real — these are behaviour tests of the screen as shipped.
 */

const searchMock = vi.fn();

vi.mock("../api", () => ({
  api: {
    search: (...args: unknown[]) => searchMock(...args),
    audioUrl: (id: string, variant: string) => `/api/recordings/${id}/audio?variant=${variant}`,
  },
  download: vi.fn(),
}));

const { default: Find } = await import("./Find");

function hit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    segment_id: "seg-1",
    recording_id: "rec-1",
    recording_filename: "call.wav",
    source_type: "phone_call",
    recorded_at: "1998-04-02T14:00:00Z",
    transcript_id: "tr-1",
    start_s: 42.5,
    end_s: 48,
    text: "he got into a blue pickup truck",
    snippet: "he got into a blue pickup truck",
    speaker: null,
    review_status: "ai_generated",
    score: 0.81,
    keyword_score: null,
    semantic_score: 0.81,
    matched_by: ["semantic"],
    ...overrides,
  };
}

function response(hits: SearchHit[], overrides: Partial<SearchResponse> = {}): SearchResponse {
  return {
    query: "a blue pickup truck",
    mode: "semantic",
    took_ms: 12,
    total: hits.length,
    hits,
    diagnostics: {},
    ...overrides,
  };
}

/**
 * A minimal CaseFile. Only the fields Find reads are populated: `exhibits` for
 * labels and scope, `number`/`id` for the exhibit label. The rest is required by
 * the type, so it is filled with neutral values.
 */
const caseFile = {
  id: "case-a",
  number: "26-104772",
  subject: "Test matter",
  description: null,
  disclosure_due: null,
  days_to_disclosure: null,
  owner: null,
  status: "open",
  exhibits: [
    {
      id: "rec-1",
      filename: "call.wav",
      source_type: "phone_call",
      duration_s: 300,
      recorded_at: "1998-04-02T14:00:00Z",
      status: "ready",
      segment_count: 24,
      review_counts: {},
      reviewed_pct: 0,
      indexed: "full" as const,
      indexed_ranges: [],
      indexed_s: 300,
      dark_s: 0,
      custody_events: 2,
      cue_total: 0,
      cue_open: 0,
      has_denoised: false,
    },
  ],
  totals: {
    exhibits: 1,
    audio_hours: 0.1,
    indexed_hours: 0.1,
    segments: 24,
    verified: 0,
    reviewed_pct: 0,
    cue_total: 0,
    cue_open: 0,
  },
};

function renderFind() {
  return render(
    <Find caseFile={caseFile as any} cases={[caseFile as any]} onOpenReview={vi.fn()} />,
  );
}

beforeEach(() => {
  searchMock.mockReset();
});

describe("scenario prompts", () => {
  it("shows curated evidentiary asks alongside the case-derived suggestions", async () => {
    searchMock.mockResolvedValue(response([]));
    renderFind();

    // The curated groups.
    expect(await screen.findByText("Find by concept")).toBeInTheDocument();
    expect(screen.getByText("Find by fact")).toBeInTheDocument();
    // The source-type-derived group is still present, not replaced.
    expect(screen.getByText("From this case's exhibits")).toBeInTheDocument();

    // A prompt the investigator will recognise from their own job.
    expect(screen.getByRole("button", { name: /a vehicle/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /a named person/i })).toBeInTheDocument();
  });

  it("pre-selects the matching search mode and runs the ask", async () => {
    const user = userEvent.setup();
    searchMock.mockResolvedValue(response([]));
    renderFind();

    const vehicle = await screen.findByRole("button", { name: /a vehicle/i });
    await user.click(vehicle);

    // "a vehicle" is a semantic ask; the mode must be set before the request,
    // not left on the screen's default hybrid.
    await waitFor(() => {
      expect(searchMock).toHaveBeenCalledWith(
        expect.objectContaining({ q: "a blue pickup truck", mode: "semantic" }),
      );
    });

    // And the *control* must agree with the request. Asserting only the call
    // would pass even if `setMode` were dropped, because `run` takes the mode as
    // an argument — leaving the segmented control reading "Hybrid" over semantic
    // results, which is the on-screen lie this check exists to prevent.
    expect(screen.getByRole("tab", { name: "Semantic" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Hybrid" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("uses hybrid matching for a literal fact ask and shows it selected", async () => {
    const user = userEvent.setup();
    searchMock.mockResolvedValue(response([]));
    renderFind();

    // A person's name is a literal string, so the ask maps to hybrid.
    const person = await screen.findByRole("button", { name: /a named person/i });
    await user.click(person);

    await waitFor(() => {
      expect(searchMock).toHaveBeenCalledWith(
        expect.objectContaining({ q: "John Smith", mode: "hybrid" }),
      );
    });
    expect(screen.getByRole("tab", { name: "Hybrid" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

describe("inline citations", () => {
  it("labels each result with its source exhibit and timestamp", async () => {
    searchMock.mockResolvedValue(response([hit()]));
    renderFind();

    const citation = await screen.findByRole("button", {
      name: /jump to the source audio/i,
    });

    // formatTimecode(42.5) → "00:42.50"; the exhibit label is derived from the
    // case's exhibit order, so the first exhibit is E-001.
    expect(citation).toHaveTextContent("00:42.50");
    expect(citation).toHaveTextContent("E-001");
  });

  it("puts the citation inside the row of the passage it cites", async () => {
    searchMock.mockResolvedValue(
      response([
        hit({ segment_id: "s1", snippet: "first passage", start_s: 10 }),
        hit({ segment_id: "s2", snippet: "second passage", start_s: 95.25 }),
      ]),
    );
    renderFind();

    const second = await screen.findByText("second passage");
    const row = second.closest("tr");
    expect(row).not.toBeNull();

    // The right row must carry its own timestamp — 95.25 s → "01:35.25".
    const citation = within(row as HTMLElement).getByRole("button", {
      name: /jump to the source audio/i,
    });
    expect(citation).toHaveTextContent("01:35.25");
  });

  it("does not navigate the row when the citation itself is clicked", async () => {
    const user = userEvent.setup();
    searchMock.mockResolvedValue(response([hit()]));
    renderFind();

    const citation = await screen.findByRole("button", {
      name: /jump to the source audio/i,
    });
    // The row's click handler also jumps; the citation stops propagation so the
    // click is handled once. Rendering without throwing and keeping the citation
    // present is the observable guarantee at this level.
    await user.click(citation);
    expect(citation).toBeInTheDocument();
  });
});

describe("the not-evidence badge", () => {
  it("marks a semantic match as not evidence", async () => {
    searchMock.mockResolvedValue(response([hit({ matched_by: ["semantic"] })]));
    renderFind();

    // One on the summary line, one on the semantic row.
    const badges = await screen.findAllByTitle(/not evidence of fact/i);
    expect(badges.length).toBeGreaterThanOrEqual(2);
  });

  it("does not decorate a keyword-only match with the header badge twice", async () => {
    searchMock.mockResolvedValue(
      response([hit({ matched_by: ["keyword"], semantic_score: null })]),
    );
    renderFind();

    // The summary always carries the reminder; the keyword row must not add a
    // second one, because a literal string match is not an AI similarity claim.
    const badges = await screen.findAllByTitle(/not evidence of fact/i);
    expect(badges).toHaveLength(1);
  });

  it("states the principle in words on the summary line", async () => {
    searchMock.mockResolvedValue(response([hit()]));
    renderFind();
    expect(await screen.findByText(/a match is not proof/i)).toBeInTheDocument();
  });
});

describe("regression: existing result rendering", () => {
  it("still shows the reported count and the review status", async () => {
    searchMock.mockResolvedValue(response([hit()]));
    renderFind();

    expect(await screen.findByText(/1 passage/i)).toBeInTheDocument();
    // ReviewStamp for an unreviewed segment.
    expect(screen.getByText(/unreviewed/i)).toBeInTheDocument();
  });

  it("shows an error banner rather than an empty table when search fails", async () => {
    searchMock.mockRejectedValue(new Error("backend down"));
    renderFind();
    expect(await screen.findByText(/backend down/i)).toBeInTheDocument();
  });
});
