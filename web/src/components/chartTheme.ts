// Shared palette and scale helpers for the custom SVG charts
// (docs/08-frontend.md). Colors resolve to the app's CSS theme tokens
// (index.css) as `var(--token)` strings — valid SVG fill/stroke values
// — so the charts follow light/dark theming with no per-chart logic.

/** Result series — the app's result tokens (index.css). */
export const WIN_COLOR = "var(--win)";
export const LOSS_COLOR = "var(--loss)";
export const DRAW_COLOR = "var(--draw)";

/** Primary single-series color: rating line, average-loss bars. */
export const PRIMARY_COLOR = "var(--accent)";

/** Axis text and labels. */
export const MUTED_COLOR = "var(--muted)";
/** Hairline gridlines. */
export const GRID_COLOR = "var(--chart-grid)";

/** Judgment scale — reuses the app's move-badge tokens (index.css). */
export const JUDGMENT_COLORS = {
  best: "var(--j-best)",
  good: "var(--j-good)",
  inaccuracy: "var(--j-inaccuracy)",
  mistake: "var(--j-mistake)",
  blunder: "var(--j-blunder)",
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

/** "Mar '26" for a "2026-03" month key — shared by the monthly charts
 *  so their x-axes read identically. */
export function formatMonth(key: string): string {
  const [year = 0, month = 1] = key.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString(undefined, {
    month: "short",
    year: "2-digit",
  });
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
