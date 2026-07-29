// Pure coverage-gap logic for the Coach page (docs/08-frontend.md,
// docs/archive/fixes-2026-07/07-analysis-coverage.md slice 4) — no fetching,
// no React, unit-tested in coachCoverage.test.ts. Mirrors explain.ts's
// split between pure reducer/selectors and the hook that wires them
// into fetch/SSE.

import { HttpError, type PlayerReport } from "./api.ts";

export type CoverageGap = {
  /** `games_analyzed` from the report — already-analyzed games in scope. */
  analyzed: number;
  /** `games_in_scope` from the report — every stored game in scope. */
  inScope: number;
};

/**
 * The report's coverage gap for its requested window/time-class, or
 * `null` when there's nothing to warn about — `games_in_scope` is
 * `null` (no scope info) or coverage is already full. Never recounts
 * client-side; both numbers come straight from the report response.
 *
 * One predicate, two call sites: it decides whether the Coach page's
 * warning renders, and — after an "Analyze the rest" run finishes —
 * whether the just-refreshed report still has a gap worth chaining
 * another run for.
 */
export function coverageGap(
  report: Pick<PlayerReport, "games_analyzed" | "games_in_scope">,
): CoverageGap | null {
  const inScope = report.games_in_scope;
  if (inScope == null || report.games_analyzed >= inScope) {
    return null;
  }
  return { analyzed: report.games_analyzed, inScope };
}

/**
 * True when a `POST /analyze` mutation failed with 409 — "a run is
 * already active for this player" (docs/07-api.md), e.g. started from
 * the Games page or a backfill CLI run. The Coach page treats this as
 * a reason to attach to the existing run's progress stream rather
 * than surface a failure.
 */
export function isRunConflict(error: unknown): boolean {
  return error instanceof HttpError && error.status === 409;
}

/**
 * Whether the auto-chain should continue past a finished run: only on
 * a clean finish. A failed run (a game whose analysis persistently
 * errors would otherwise sit at the head of
 * `games_needing_analysis` forever) or a lost progress stream (the
 * run's true state is unknown — it may still be running, may be gone)
 * both stop the chain, so the same broken game or connection can't
 * re-fire an unbounded fail → re-read → fail loop. The user resumes
 * manually with "Analyze the rest" in either case, matching the
 * stream-lost UI text ("…to continue").
 */
export function shouldChainAfterRun(outcome: {
  failed: boolean;
  streamLost: boolean;
}): boolean {
  return !outcome.failed && !outcome.streamLost;
}
