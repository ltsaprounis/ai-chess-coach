import {
  axisScale,
  formatMonth,
  GRID_COLOR,
  MUTED_COLOR,
  PRIMARY_COLOR,
} from "./chartTheme.ts";

const VB_WIDTH = 640;
const VB_HEIGHT = 220;
const MARGIN = { top: 12, right: 12, bottom: 26, left: 40 };
const INNER_WIDTH = VB_WIDTH - MARGIN.left - MARGIN.right;
const INNER_HEIGHT = VB_HEIGHT - MARGIN.top - MARGIN.bottom;

export type MonthPoint = { month: string; value: number | null };

type Props = {
  data: MonthPoint[];
  /** Accessible name, e.g. "ACPL by month". */
  label: string;
  /** Formats the hover/tooltip value, e.g. "12.3%" for blunder rate. */
  formatValue?: (value: number) => string;
};

/**
 * A metric per calendar month, oldest first — months the player is
 * covered for report the trend (ACPL, blunder rate); months with no
 * analyzed games are `null` and simply break the line rather than
 * plotting as zero, which would read as flawless play.
 */
export default function MonthlyMetricChart({
  data,
  label,
  formatValue,
}: Props) {
  if (data.length === 0) {
    return null;
  }
  const format = formatValue ?? ((value: number) => String(value));

  const values = data
    .map((point) => point.value)
    .filter((value): value is number => value !== null);
  if (values.length === 0) {
    return <p className="panel-empty">No analyzed games in this period yet.</p>;
  }

  const scale = axisScale(0, Math.max(...values), { zeroBased: true });
  const slot = INNER_WIDTH / data.length;
  const x = (index: number): number => MARGIN.left + slot * (index + 0.5);
  const y = (value: number): number =>
    MARGIN.top + INNER_HEIGHT - (value / scale.hi) * INNER_HEIGHT;

  // Split into runs of consecutive non-null points so the line breaks
  // over months with no analyzed games, rather than interpolating
  // across them or reading as a zero.
  const runs: { month: string; index: number; value: number }[][] = [];
  let current: { month: string; index: number; value: number }[] = [];
  data.forEach((point, index) => {
    if (point.value === null) {
      if (current.length > 0) {
        runs.push(current);
        current = [];
      }
      return;
    }
    current.push({ month: point.month, index, value: point.value });
  });
  if (current.length > 0) {
    runs.push(current);
  }

  const labelEvery = Math.ceil(data.length / 8);

  return (
    <div className="chart-wrap">
      <svg
        viewBox={`0 0 ${VB_WIDTH} ${VB_HEIGHT}`}
        role="img"
        aria-label={label}
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
        {runs.map((run) => (
          <polyline
            key={run[0]?.month ?? ""}
            points={run
              .map((point) => `${x(point.index)},${y(point.value)}`)
              .join(" ")}
            fill="none"
            stroke={PRIMARY_COLOR}
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}
        {data.map((point, index) =>
          point.value === null ? null : (
            <circle
              key={point.month}
              cx={x(index)}
              cy={y(point.value)}
              r={3}
              fill={PRIMARY_COLOR}
            >
              <title>
                {formatMonth(point.month)}: {format(point.value)}
              </title>
            </circle>
          ),
        )}
        {data.map(
          (point, index) =>
            (index % labelEvery === 0 || index === data.length - 1) && (
              <text
                key={point.month}
                x={x(index)}
                y={VB_HEIGHT - 8}
                textAnchor="middle"
                fontSize={11}
                fill={MUTED_COLOR}
              >
                {formatMonth(point.month)}
              </text>
            ),
        )}
      </svg>
    </div>
  );
}
