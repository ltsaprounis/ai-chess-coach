import { useState } from "react";
import type { RatingPoint } from "../stats.ts";
import {
  axisScale,
  GRID_COLOR,
  MUTED_COLOR,
  PRIMARY_COLOR,
} from "./chartTheme.ts";

const VB_WIDTH = 640;
const VB_HEIGHT = 260;
const MARGIN = { top: 12, right: 12, bottom: 26, left: 48 };
const INNER_WIDTH = VB_WIDTH - MARGIN.left - MARGIN.right;
const INNER_HEIGHT = VB_HEIGHT - MARGIN.top - MARGIN.bottom;

const dateLabel = (endTime: number): string =>
  new Date(endTime * 1000).toLocaleDateString();

type Props = {
  points: RatingPoint[];
  /** Accessible name, e.g. "blitz rating over time". */
  label: string;
};

/** Rating per game over time; hover for a crosshair + exact value. */
export default function RatingChart({ points, label }: Props) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  if (points.length === 0) {
    return null;
  }

  const ratings = points.map((point) => point.rating);
  const scale = axisScale(Math.min(...ratings), Math.max(...ratings), {
    minPad: 10,
    minStep: 5,
  });
  const t0 = points[0]?.endTime ?? 0;
  const t1 = points[points.length - 1]?.endTime ?? t0;
  const timeSpan = Math.max(1, t1 - t0);
  const x = (endTime: number): number =>
    points.length === 1
      ? MARGIN.left + INNER_WIDTH / 2
      : MARGIN.left + ((endTime - t0) / timeSpan) * INNER_WIDTH;
  const y = (rating: number): number =>
    MARGIN.top +
    INNER_HEIGHT -
    ((rating - scale.lo) / (scale.hi - scale.lo)) * INNER_HEIGHT;

  const line = points
    .map((point) => `${x(point.endTime)},${y(point.rating)}`)
    .join(" ");

  // Sparse time labels: first, middle, last (deduped when they repeat).
  const tickTimes = points.length > 1 ? [t0, t0 + timeSpan / 2, t1] : [t0];
  const xTicks = tickTimes
    .map((endTime) => ({ endTime, text: dateLabel(endTime) }))
    .filter(
      (tick, index, all) => index === 0 || tick.text !== all[index - 1]?.text,
    );
  const anchor = (index: number, count: number): "start" | "end" | "middle" =>
    index === 0 ? "start" : index === count - 1 ? "end" : "middle";

  const locate = (event: React.MouseEvent<SVGSVGElement>): void => {
    const rect = event.currentTarget.getBoundingClientRect();
    const viewX = ((event.clientX - rect.left) / rect.width) * VB_WIDTH;
    let nearest = 0;
    let best = Number.POSITIVE_INFINITY;
    for (const [index, point] of points.entries()) {
      const distance = Math.abs(x(point.endTime) - viewX);
      if (distance < best) {
        best = distance;
        nearest = index;
      }
    }
    setHoverIndex(nearest);
  };
  const hover = hoverIndex === null ? null : (points[hoverIndex] ?? null);

  return (
    <div className="chart-wrap">
      <svg
        viewBox={`0 0 ${VB_WIDTH} ${VB_HEIGHT}`}
        role="img"
        aria-label={label}
        onMouseMove={locate}
        onMouseLeave={() => setHoverIndex(null)}
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
        <text
          transform={`rotate(-90 10 ${VB_HEIGHT / 2})`}
          x={10}
          y={VB_HEIGHT / 2}
          textAnchor="middle"
          fontSize={11}
          fill={MUTED_COLOR}
        >
          rating
        </text>
        {xTicks.map((tick, index) => (
          <text
            key={tick.endTime}
            x={x(tick.endTime)}
            y={VB_HEIGHT - 8}
            textAnchor={anchor(index, xTicks.length)}
            fontSize={11}
            fill={MUTED_COLOR}
          >
            {tick.text}
          </text>
        ))}
        <polyline
          points={line}
          fill="none"
          stroke={PRIMARY_COLOR}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {hover && (
          <g>
            <line
              x1={x(hover.endTime)}
              y1={MARGIN.top}
              x2={x(hover.endTime)}
              y2={MARGIN.top + INNER_HEIGHT}
              stroke={MUTED_COLOR}
              strokeWidth={1}
            />
            <circle
              cx={x(hover.endTime)}
              cy={y(hover.rating)}
              r={3.5}
              fill={PRIMARY_COLOR}
            />
          </g>
        )}
      </svg>
      {hover && (
        <div
          className="chart-tooltip"
          style={{
            left: `clamp(3rem, ${(x(hover.endTime) / VB_WIDTH) * 100}%, calc(100% - 3rem))`,
            top: `${(y(hover.rating) / VB_HEIGHT) * 100}%`,
          }}
        >
          {dateLabel(hover.endTime)} — {hover.rating}
        </div>
      )}
    </div>
  );
}
