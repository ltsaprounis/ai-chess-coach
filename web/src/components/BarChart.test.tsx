import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { formatPawns, LOSS_AXIS_STEP } from "../units.ts";
import BarChart from "./BarChart.tsx";

// Same "no DOM" constraint as the other component tests (the Vitest
// environment is "node", per vite.config.ts): these assert on rendered
// markup for given props.

/** The y-axis tick labels, in render order. */
function ticks(markup: string): string[] {
  return [...markup.matchAll(/text-anchor="end"[^>]*>([^<]*)</g)].map(
    (match) => match[1] ?? "",
  );
}

describe("BarChart axis", () => {
  it("defaults to whole-number steps, which is right for counts", () => {
    const markup = renderToStaticMarkup(
      <BarChart
        data={[
          { label: "blunder", value: 6 },
          { label: "mistake", value: 7 },
        ]}
        label="moves per judgment"
      />,
    );

    expect(ticks(markup)).toEqual(["0", "2", "4", "6"]);
  });

  it("labels a centipawn series in pawns without rescaling the bars", () => {
    // The phase chart's real shape. Values stay in the API's
    // centipawns (stats.ts) and only the axis text converts, so the
    // bars keep their ratios and no pawn-scale number is in flight for
    // the formatter to divide a second time.
    const markup = renderToStaticMarkup(
      <BarChart
        data={[
          { label: "opening", value: 14 },
          { label: "middlegame", value: 32 },
          { label: "endgame", value: 20 },
        ]}
        label="average pawns lost per move by phase"
        minStep={LOSS_AXIS_STEP}
        formatValue={formatPawns}
      />,
    );

    expect(ticks(markup)).toEqual(["0.00", "0.10", "0.20", "0.30"]);
  });

  it("formats the hover value rather than dumping the raw float", () => {
    // 107/100 is not exactly representable; without the formatter the
    // tooltip reads "1.0700000000000003".
    const markup = renderToStaticMarkup(
      <BarChart
        data={[{ label: "opening", value: 107 }]}
        label="average pawns lost per move by phase"
        minStep={LOSS_AXIS_STEP}
        formatValue={formatPawns}
      />,
    );

    expect(markup).toContain("<title>opening: 1.07</title>");
    expect(markup).not.toContain("1.0700");
  });

  it("still renders a sane axis when every bar is zero", () => {
    const markup = renderToStaticMarkup(
      <BarChart
        data={[{ label: "opening", value: 0 }]}
        label="average pawns lost per move by phase"
        minStep={LOSS_AXIS_STEP}
        formatValue={formatPawns}
      />,
    );

    expect(ticks(markup)).toEqual(["0.00", "0.10"]);
  });
});
