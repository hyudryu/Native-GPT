import { describe, expect, it } from "vitest";
import { orchestrationActivityLabel, parseOrchestration } from "./runStore";

describe("parseOrchestration", () => {
  it("parses a full run.orchestration payload (design spec §5)", () => {
    const parsed = parseOrchestration({
      run_id: "run-1",
      conversation_id: "conv-1",
      state: "INVESTIGATE",
      steps: [
        { id: "frame", label: "Framed the problem", status: "complete" },
        {
          id: "sp-memory",
          label: "Investigating memory comparison",
          status: "running",
          detail: { worker: "worker-2", tools_used: ["web-search"] },
        },
        { id: "synthesize", label: "Synthesize", status: "pending" },
      ],
      budgets: { tokens_used: 48200, token_budget: 120000, elapsed_s: 140, time_budget_s: 600 },
    });
    expect(parsed).not.toBeNull();
    expect(parsed!.state).toBe("INVESTIGATE");
    expect(parsed!.steps).toHaveLength(3);
    expect(parsed!.steps[1]).toMatchObject({ id: "sp-memory", status: "running" });
    expect(parsed!.budgets).toEqual({
      tokens_used: 48200,
      token_budget: 120000,
      elapsed_s: 140,
      time_budget_s: 600,
    });
    expect(parsed!.synthesizeRequested).toBe(false);
  });

  it("returns null when the state is missing", () => {
    expect(parseOrchestration({ steps: [], budgets: {} })).toBeNull();
  });

  it("tolerates missing budgets and malformed steps", () => {
    const parsed = parseOrchestration({
      state: "FRAME",
      steps: [{ id: "frame", label: "Frame", status: "exploding" }, "junk", null],
    });
    expect(parsed).not.toBeNull();
    expect(parsed!.budgets).toEqual({
      tokens_used: 0,
      token_budget: 0,
      elapsed_s: 0,
      time_budget_s: 0,
    });
    // Unknown status degrades to pending; non-object entries are dropped.
    expect(parsed!.steps).toEqual([{ id: "frame", label: "Frame", status: "pending" }]);
  });

  it("accepts every documented step status", () => {
    const parsed = parseOrchestration({
      state: "COMPLETE",
      steps: ["pending", "running", "complete", "failed", "skipped"].map((status, i) => ({
        id: `s${i}`,
        label: `Step ${i}`,
        status,
      })),
    });
    expect(parsed!.steps.map((step) => step.status)).toEqual([
      "pending",
      "running",
      "complete",
      "failed",
      "skipped",
    ]);
  });
});

describe("orchestrationActivityLabel", () => {
  it("has a label for every documented state", () => {
    for (const state of [
      "FRAME",
      "DECOMPOSE",
      "INVESTIGATE",
      "REVIEW",
      "CRITIQUE",
      "RESOLVE",
      "SYNTHESIZE",
      "COMPLETE",
      "FAILED",
    ]) {
      expect(orchestrationActivityLabel(state).length).toBeGreaterThan(0);
    }
  });

  it("falls back gracefully for unknown states", () => {
    expect(orchestrationActivityLabel("WHATEVER")).toContain("whatever");
  });
});
