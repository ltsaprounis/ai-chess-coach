// Pure helpers for the Game page's live-eval panel — no fetching,
// no React, unit-tested in liveEval.test.ts.

/**
 * One SSE `eval` event from `GET /api/eval`. Hand-declared because
 * SSE payloads are not part of the OpenAPI schema; mirrors the
 * backend's LiveEval model (white's perspective, mate as signed
 * moves to mate).
 */
export type LiveEval = {
  depth: number;
  eval_cp: number | null;
  eval_mate: number | null;
  pv_san: string[];
};

type Score = Pick<LiveEval, "eval_cp" | "eval_mate">;

/** Score as text: cp as ±x.xx pawns, mate as "M n" / "−M n". */
export function formatEval(score: Score): string {
  if (score.eval_mate !== null) {
    return score.eval_mate >= 0
      ? `M ${score.eval_mate}`
      : `−M ${-score.eval_mate}`;
  }
  const pawns = (score.eval_cp ?? 0) / 100;
  const magnitude = Math.abs(pawns).toFixed(2);
  if (magnitude === "0.00") {
    return "0.00";
  }
  return pawns > 0 ? `+${magnitude}` : `−${magnitude}`;
}

/**
 * Eval-bar fill toward white, 0..1 with 0.5 = equal. Logistic in cp
 * so the bar saturates smoothly instead of pinning at a hard clamp;
 * mate fills the bar for the mating side.
 */
export function whiteFraction(score: Score): number {
  if (score.eval_mate !== null) {
    return score.eval_mate >= 0 ? 1 : 0;
  }
  return 1 / (1 + Math.exp(-(score.eval_cp ?? 0) / 250));
}
