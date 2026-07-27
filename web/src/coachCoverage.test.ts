import { describe, expect, it } from "vitest";
import { HttpError } from "./api";
import {
  coverageGap,
  isRunConflict,
  shouldChainAfterRun,
} from "./coachCoverage";

// coverageGap backs two Coach-page decisions: whether the "N of M
// games analyzed" warning renders, and — after an "Analyze the rest"
// run finishes — whether the refreshed report still has a gap worth
// chaining another run for. Same predicate, exercised at both points.
describe("coverageGap", () => {
  it("reports the gap when fewer games are analyzed than are in scope", () => {
    expect(coverageGap({ games_analyzed: 450, games_in_scope: 1025 })).toEqual({
      analyzed: 450,
      inScope: 1025,
    });
  });

  it("is null once analyzed coverage reaches the in-scope count (chain stops, warning clears)", () => {
    expect(
      coverageGap({ games_analyzed: 1025, games_in_scope: 1025 }),
    ).toBeNull();
  });

  it("is null when games_in_scope is null (no scope info)", () => {
    expect(
      coverageGap({ games_analyzed: 450, games_in_scope: null }),
    ).toBeNull();
  });

  it("is null when games_in_scope is undefined", () => {
    expect(coverageGap({ games_analyzed: 450 })).toBeNull();
  });

  it("is null when analyzed somehow exceeds in-scope (never warns backwards)", () => {
    expect(coverageGap({ games_analyzed: 10, games_in_scope: 5 })).toBeNull();
  });
});

describe("isRunConflict", () => {
  it("is true for a 409 (a run is already active elsewhere)", () => {
    expect(isRunConflict(new HttpError(409, "a run is active"))).toBe(true);
  });

  it("is false for other HTTP errors", () => {
    expect(isRunConflict(new HttpError(500, "boom"))).toBe(false);
  });

  it("is false for a non-HTTP error", () => {
    expect(isRunConflict(new Error("network down"))).toBe(false);
  });
});

// Gates the Coach page's auto-chain (fired from useAnalysisProgress's
// onFinished): a persistently-failing game sits at the head of
// games_needing_analysis forever, so chaining past a failure would
// hot-loop with no backoff (fail -> re-read -> gap remains -> new run
// -> fail -> ...). A lost stream leaves the run's true state unknown.
// Both stop the chain; only a clean finish may continue it, and even
// then only when coverageGap still finds a gap (tested above).
describe("shouldChainAfterRun", () => {
  it("does not chain after a run_failed finish", () => {
    expect(shouldChainAfterRun({ failed: true, streamLost: false })).toBe(
      false,
    );
  });

  it("does not chain after the progress stream is lost", () => {
    expect(shouldChainAfterRun({ failed: false, streamLost: true })).toBe(
      false,
    );
  });

  it("does not chain when a run somehow reports both failed and lost", () => {
    expect(shouldChainAfterRun({ failed: true, streamLost: true })).toBe(false);
  });

  it("allows chaining after a clean finish", () => {
    expect(shouldChainAfterRun({ failed: false, streamLost: false })).toBe(
      true,
    );
  });
});
