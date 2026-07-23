// Shared palette and scale helpers for the custom SVG charts
// (docs/08-frontend.md) — every chart color is defined here once.

/** Result series — the app's existing result colors (index.css). */
export const WIN_COLOR = "#15803d";
export const LOSS_COLOR = "#b91c1c";
export const DRAW_COLOR = "#898781";

/** Primary single-series color: rating line, ACPL bars. */
export const PRIMARY_COLOR = "#2a78d6";

/** Axis text and labels. */
export const MUTED_COLOR = "#898781";
/** Hairline gridlines. */
export const GRID_COLOR = "#e1e0d9";

/** Judgment scale — reuses the app's move-badge tints (index.css). */
export const JUDGMENT_COLORS = {
  best: WIN_COLOR,
  good: DRAW_COLOR,
  inaccuracy: "#eab308",
  mistake: "#f97316",
  blunder: "#dc2626",
} as const;

export type Scale = { lo: number; hi: number; ticks: number[] };

/** Smallest of 1/2/5 × 10^n at or above the rough step. */
function niceStep(rough: number): number {
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  if (normalized <= 1) {
    return magnitude;
  }
  if (normalized <= 2) {
    return 2 * magnitude;
  }
  if (normalized <= 5) {
    return 5 * magnitude;
  }
  return 10 * magnitude;
}

/**
 * Y-axis scale with nice tick values: zero-based for counts, padded
 * around the data otherwise (ratings never get forced to zero).
 */
export function axisScale(
  minValue: number,
  maxValue: number,
  options: { zeroBased?: boolean; minPad?: number; minStep?: number } = {},
): Scale {
  let lo = options.zeroBased ? Math.min(0, minValue) : minValue;
  let hi = maxValue;
  if (hi <= lo) {
    hi = lo + 1;
  }
  if (!options.zeroBased) {
    const pad = Math.max((hi - lo) * 0.1, options.minPad ?? 0);
    lo -= pad;
    hi += pad;
  }
  const step = Math.max(options.minStep ?? 0, niceStep((hi - lo) / 4));
  const ticks: number[] = [];
  for (
    let tick = Math.ceil(lo / step) * step;
    tick <= hi + step / 1e6;
    tick += step
  ) {
    ticks.push(Number(tick.toFixed(6)));
  }
  return { lo, hi, ticks };
}

/** Rect path rounded only at the top — the data end of an upward bar. */
export function topRoundedRectPath(
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): string {
  const r = Math.max(0, Math.min(radius, height, width / 2));
  return [
    `M${x},${y + height}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${y + height}`,
    "Z",
  ].join(" ");
}
