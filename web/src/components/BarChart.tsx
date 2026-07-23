import {
  axisScale,
  GRID_COLOR,
  MUTED_COLOR,
  PRIMARY_COLOR,
  topRoundedRectPath,
} from "./chartTheme.ts";

const VB_WIDTH = 480;
const VB_HEIGHT = 220;
const MARGIN = { top: 12, right: 12, bottom: 26, left: 36 };
const INNER_WIDTH = VB_WIDTH - MARGIN.left - MARGIN.right;
const INNER_HEIGHT = VB_HEIGHT - MARGIN.top - MARGIN.bottom;

export type BarDatum = {
  label: string;
  value: number;
  color?: string;
};

type Props = {
  data: BarDatum[];
  /** Accessible name, e.g. "average centipawn loss by phase". */
  label: string;
};

/** Zero-based vertical bars for a handful of categories. */
export default function BarChart({ data, label }: Props) {
  if (data.length === 0) {
    return null;
  }

  const maxValue = Math.max(1, ...data.map((datum) => datum.value));
  const scale = axisScale(0, maxValue, { zeroBased: true, minStep: 1 });
  const y = (value: number): number =>
    MARGIN.top + INNER_HEIGHT - (value / scale.hi) * INNER_HEIGHT;

  const slot = INNER_WIDTH / data.length;
  const barWidth = Math.max(2, Math.min(56, slot - 2));

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
        {data.map((datum, index) => {
          const barX = MARGIN.left + index * slot + (slot - barWidth) / 2;
          const top = y(datum.value);
          const title = `${datum.label}: ${datum.value}`;
          return (
            <g key={datum.label}>
              {datum.value > 0 && (
                <path
                  d={topRoundedRectPath(barX, top, barWidth, y(0) - top, 4)}
                  fill={datum.color ?? PRIMARY_COLOR}
                />
              )}
              {/* Full-column hit target so the tooltip is easy to reach. */}
              <rect
                x={MARGIN.left + index * slot}
                y={MARGIN.top}
                width={slot}
                height={INNER_HEIGHT}
                fill="transparent"
              >
                <title>{title}</title>
              </rect>
              <text
                x={barX + barWidth / 2}
                y={VB_HEIGHT - 8}
                textAnchor="middle"
                fontSize={11}
                fill={MUTED_COLOR}
              >
                {datum.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
