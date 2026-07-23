// Pure helpers for the Game page's live-eval panel — no fetching,
// no React, unit-tested in liveEval.test.ts.

/**
 * One MultiPV candidate line, white's POV; the score assumes its
 * first move (`pv_san[0]`) is played. Hand-declared because SSE
 * payloads are not part of the OpenAPI schema; mirrors the backend's
 * `EvalLine` domain model.
 */
export type EvalLine = {
  multipv: number;
  depth: number;
  eval_cp: number | null;
  eval_mate: number | null;
  pv_san: string[];
};

/**
 * One SSE `eval` event from `GET /api/eval` — a snapshot of the
 * current MultiPV candidate lines, sorted by multipv rank.
 * Hand-declared because SSE payloads are not part of the OpenAPI
 * schema; mirrors the backend's `LiveEval` model.
 */
export type LiveEval = {
  lines: EvalLine[];
};

type Score = Pick<EvalLine, "eval_cp" | "eval_mate">;

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

/** Which side a score favors — drives the candidate-row eval chip's color. */
export type EvalSign = "white" | "black" | "equal";

/** Positive/mate-for-white favors White, negative/mate-for-black favors Black. */
export function evalSign(score: Score): EvalSign {
  if (score.eval_mate !== null) {
    return score.eval_mate >= 0 ? "white" : "black";
  }
  const cp = score.eval_cp ?? 0;
  if (cp === 0) {
    return "equal";
  }
  return cp > 0 ? "white" : "black";
}

/** Side to move from a FEN's active-color field ("w"/"b", 2nd space-separated token). */
export function sideToMove(fen: string): "white" | "black" {
  return fen.split(" ")[1] === "b" ? "black" : "white";
}
