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
/** Extra headroom reserved above the bars when any datum carries a
 *  `note` — without it, a bar near the max value leaves no room for
 *  its label and the two overlap illegibly. */
const NOTE_HEADROOM = 16;

export type BarDatum = {
  label: string;
  value: number;
  color?: string;
  /** Small annotation shown above the bar, e.g. "412 moves" — the
   *  sample size behind the value, so a thin sample is visible rather
   *  than read as a flat, confident number. */
  note?: string;
};

type Props = {
  data: BarDatum[];
  /** Accessible name, e.g. "average pawns lost per move by phase". */
  label: string;
  /**
   * Smallest axis step, and the floor the axis is scaled to when every
   * bar is smaller. Defaults to 1, which is right for counts (half a
   * blunder is not a tick). Fractional series must lower it: pawn
   * losses live between roughly 0.1 and 1.5, and a step of 1 collapses
   * the whole series onto the bottom gridline.
   */
  minStep?: number;
  /** Formats tick labels and the hover value. Defaults to `String`,
   *  which is only safe for integers — a converted value needs fixed
   *  decimals or binary float noise reaches the tooltip. */
  formatValue?: (value: number) => string;
};

/** Zero-based vertical bars for a handful of categories. */
export default function BarChart({
  data,
  label,
  minStep = 1,
  formatValue,
}: Props) {
  if (data.length === 0) {
    return null;
  }
  const format = formatValue ?? ((value: number) => String(value));

  const maxValue = Math.max(minStep, ...data.map((datum) => datum.value));
  const scale = axisScale(0, maxValue, { zeroBased: true, minStep });
  const hasNotes = data.some((datum) => datum.note !== undefined);
  const topMargin = MARGIN.top + (hasNotes ? NOTE_HEADROOM : 0);
  const innerHeight = VB_HEIGHT - topMargin - MARGIN.bottom;
  const y = (value: number): number =>
    topMargin + innerHeight - (value / scale.hi) * innerHeight;

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
              {format(tick)}
            </text>
          </g>
        ))}
        {data.map((datum, index) => {
          const barX = MARGIN.left + index * slot + (slot - barWidth) / 2;
          const top = y(datum.value);
          const title = `${datum.label}: ${format(datum.value)}`;
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
                y={topMargin}
                width={slot}
                height={innerHeight}
                fill="transparent"
              >
                <title>{title}</title>
              </rect>
              {datum.note !== undefined && (
                <text
                  x={barX + barWidth / 2}
                  y={top - 5}
                  textAnchor="middle"
                  fontSize={9.5}
                  fill={MUTED_COLOR}
                >
                  {datum.note}
                </text>
              )}
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
