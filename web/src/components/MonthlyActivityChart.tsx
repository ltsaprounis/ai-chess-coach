import type { MonthActivity } from "../stats.ts";
import {
  axisScale,
  DRAW_COLOR,
  GRID_COLOR,
  LOSS_COLOR,
  MUTED_COLOR,
  topRoundedRectPath,
  WIN_COLOR,
} from "./chartTheme.ts";

const VB_WIDTH = 640;
const VB_HEIGHT = 260;
const MARGIN = { top: 12, right: 12, bottom: 26, left: 36 };
const INNER_WIDTH = VB_WIDTH - MARGIN.left - MARGIN.right;
const INNER_HEIGHT = VB_HEIGHT - MARGIN.top - MARGIN.bottom;
const SEGMENT_GAP = 2;

const SERIES = [
  { key: "wins", label: "wins", color: WIN_COLOR },
  { key: "draws", label: "draws", color: DRAW_COLOR },
  { key: "losses", label: "losses", color: LOSS_COLOR },
] as const;

function formatMonth(key: string): string {
  const [year = 0, month = 1] = key.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString(undefined, {
    month: "short",
    year: "2-digit",
  });
}

type Props = {
  data: MonthActivity[];
};

/** Games per month, stacked by result (wins at the base). */
export default function MonthlyActivityChart({ data }: Props) {
  if (data.length === 0) {
    return null;
  }

  const maxTotal = Math.max(1, ...data.map((month) => month.games));
  const scale = axisScale(0, maxTotal, { zeroBased: true, minStep: 1 });
  const y = (value: number): number =>
    MARGIN.top + INNER_HEIGHT - (value / scale.hi) * INNER_HEIGHT;

  const slot = INNER_WIDTH / data.length;
  const barWidth = Math.max(2, Math.min(40, slot - 2));
  const barX = (index: number): number =>
    MARGIN.left + index * slot + (slot - barWidth) / 2;
  const labelEvery = Math.ceil(data.length / 8);

  return (
    <div className="chart-wrap">
      <div className="legend">
        {SERIES.map((series) => (
          <span key={series.key}>
            <span className="swatch" style={{ background: series.color }} />
            {series.label}
          </span>
        ))}
      </div>
      <svg
        viewBox={`0 0 ${VB_WIDTH} ${VB_HEIGHT}`}
        role="img"
        aria-label="games per month by result"
      >
        {scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={MARGIN.left}
              y1={y(tick)}
              x2={VB_WIDTH - MARGIN.right}
              y2={y(tick)}
              stroke={GRID_COLOR}
              strokeWidth={1}
            />
            <text
              x={MARGIN.left - 6}
              y={y(tick)}
              textAnchor="end"
              dominantBaseline="middle"
              fontSize={11}
              fill={MUTED_COLOR}
            >
              {tick}
            </text>
          </g>
        ))}
        {data.map((month, index) => {
          const stack = SERIES.map((series) => ({
            ...series,
            value: month[series.key],
          })).filter((segment) => segment.value > 0);
          let cumulative = 0;
          const topKey = stack[stack.length - 1]?.key;
          return (
            <g key={month.month}>
              {stack.map((segment) => {
                const bottom = y(cumulative);
                cumulative += segment.value;
                const top = y(cumulative);
                const isTop = segment.key === topKey;
                // ~2px gap carved from the top of every non-top segment.
                const gap = isTop ? 0 : SEGMENT_GAP;
                const height = Math.max(1, bottom - top - gap);
                const title = `${formatMonth(month.month)}: ${segment.value} ${segment.label}`;
                return isTop ? (
                  <path
                    key={segment.key}
                    d={topRoundedRectPath(
                      barX(index),
                      top,
                      barWidth,
                      height,
                      4,
                    )}
                    fill={segment.color}
                  >
                    <title>{title}</title>
                  </path>
                ) : (
                  <rect
                    key={segment.key}
                    x={barX(index)}
                    y={top + gap}
                    width={barWidth}
                    height={height}
                    fill={segment.color}
                  >
                    <title>{title}</title>
                  </rect>
                );
              })}
              {(index % labelEvery === 0 || index === data.length - 1) && (
                <text
                  x={barX(index) + barWidth / 2}
                  y={VB_HEIGHT - 8}
                  textAnchor="middle"
                  fontSize={11}
                  fill={MUTED_COLOR}
                >
                  {formatMonth(month.month)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
