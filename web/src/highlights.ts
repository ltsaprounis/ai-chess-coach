// Pure helpers for the Dashboard's highlight tables (blunders +
// brilliancies) — no fetching, no React, unit-tested in
// highlights.test.ts.

import type { HighlightMove } from "./api.ts";

/** The `HighlightMove` fields the move-label formatter needs. */
export type HighlightMoveLabel = Pick<
  HighlightMove,
  "move_number" | "san" | "color"
>;

/** The `HighlightMove` fields the player-POV eval fold needs. */
export type HighlightMoveEval = Pick<
  HighlightMove,
  "eval_after_cp" | "eval_after_mate" | "color"
>;

/**
 * The move as a PGN reader would render it, e.g. "26.Nb6" as White or
 * "26...Nb6" as Black — `move_number`/`san` already come from the
 * backend per-move, so this only needs to pick the right side's
 * separator.
 */
export function moveLabel(move: HighlightMoveLabel): string {
  return move.color === "white"
    ? `${move.move_number}.${move.san}`
    : `${move.move_number}...${move.san}`;
}

/** A cp/mate score pair from the player's point of view. */
export type PlayerPovEval = { cp: number | null; mate: number | null };

/**
 * Folds `HighlightMove`'s white-POV eval-after fields (like
 * `MoveEval`, per docs/06-coach.md) to the player's point of view —
 * negating both for a Black row, since a Black player's good move
 * shows as a *negative* white-POV eval.
 */
export function foldToPlayerPov(move: HighlightMoveEval): PlayerPovEval {
  const sign = move.color === "black" ? -1 : 1;
  return {
    cp: move.eval_after_cp === null ? null : move.eval_after_cp * sign,
    mate: move.eval_after_mate === null ? null : move.eval_after_mate * sign,
  };
}

/**
 * Player-POV eval after the move, formatted for the highlights table:
 * signed pawns to two decimals, or "#N" for a mate the player's side
 * delivers ("-#N" for the rarer case of a mate against the player
 * showing up here). Null cp with no mate reads as "0.00" — an even
 * position, not a missing value.
 */
export function formatPlayerEval(pov: PlayerPovEval): string {
  if (pov.mate !== null) {
    return pov.mate >= 0 ? `#${pov.mate}` : `-#${-pov.mate}`;
  }
  const pawns = (pov.cp ?? 0) / 100;
  const magnitude = Math.abs(pawns).toFixed(2);
  if (magnitude === "0.00") {
    return "0.00";
  }
  return pawns > 0 ? `+${magnitude}` : `-${magnitude}`;
}

/** The blunder table's number column: how much the move lost, always
 *  shown as a loss regardless of color (unlike the brilliancies'
 *  player-POV eval, `cp_loss` is already color-agnostic). */
export function formatCpLoss(cpLoss: number): string {
  return `-${cpLoss}`;
}
