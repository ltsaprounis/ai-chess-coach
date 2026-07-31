/**
 * Pawns, everywhere the user can see (docs/08-frontend.md, "Units").
 *
 * The API speaks centipawns — `overall_acpl`, `PhaseStats.acpl`,
 * `MonthStats.acpl`, `PeriodStats.acpl`, `OpeningStats.opening_acpl`
 * and `avg_cp_loss`, `MoveEval.cp_loss` are all centipawn-scale, and
 * storage keeps them that way because integer centipawns are what the
 * engine emits. Every one of them becomes pawns *here*, at the render
 * edge, and nowhere else: aggregation and sorting stay on the raw
 * numbers, where a factor of 100 cancels and precision does not.
 *
 * Why pawns rather than the raw figure: the audience plays on
 * chess.com, where the eval bar is pawns and "ACPL" is not house
 * vocabulary, and a single move's cost has to be comparable with the
 * per-move average for either to teach anything ("0.32 a move, and
 * this one cost 3.1"). The coach's own prose is pawns for the same
 * reason (docs/06-coach.md, style contract), and it is rendered beside
 * these numbers on the same screens.
 *
 * These mirror the backend's renderers deliberately, decimals
 * included: `formatPawns` is `coach/prompt.py::_pawns_or_na` and
 * `formatPawnLoss` is `coach/prompt.py::format_cp_loss`, so one figure
 * reads the same in a table cell as in the advice paragraph under it.
 */

/**
 * Minimum axis step for a loss chart, **in centipawns** — the unit the
 * chart's own values keep. Ten, so the pawn-formatted axis never offers
 * a step finer than 0.10: below that the gridlines claim precision the
 * sample does not have, and a series that is all zeroes still gets a
 * sane 0.00–0.10 axis rather than a degenerate one.
 */
export const LOSS_AXIS_STEP = 10;

/** Deliberately not exported. Handing out a bare pawn number is how a
 *  value gets divided by a hundred twice — once by its producer and
 *  again by whatever formats it. Everything leaving this module is a
 *  string, so there is no pawn-scale number in flight to convert. */
function toPawns(centipawns: number): number {
  return centipawns / 100;
}

/** An average or aggregate loss, two decimals, no sign — every value
 *  this formats is a magnitude (a cost), never a signed eval. Two
 *  decimals because these cluster between roughly 0.15 and 1.50, where
 *  one decimal would flatten a real difference in strength. */
export function formatPawns(centipawns: number): string {
  return toPawns(centipawns).toFixed(2);
}

/** One move's loss, one decimal, no sign. Coarser than `formatPawns`
 *  on purpose and for the same reason the coach says "about 3.1
 *  pawns": a single move's cost is an order-of-magnitude fact, and a
 *  second decimal on it is false precision. */
export function formatPawnLoss(centipawns: number): string {
  return toPawns(centipawns).toFixed(1);
}
