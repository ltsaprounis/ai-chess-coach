import { describe, expect, it } from "vitest";
import { formatPawnLoss, formatPawns } from "./units.ts";

describe("formatPawns", () => {
  it("converts centipawns to pawns with two decimals", () => {
    expect(formatPawns(35)).toBe("0.35");
    expect(formatPawns(142)).toBe("1.42");
  });

  it("renders zero as 0.00", () => {
    expect(formatPawns(0)).toBe("0.00");
  });

  it("keeps two decimals rather than flattening a real difference", () => {
    // 0.14 vs 0.20 is opening-vs-middlegame in the scenario fixture; at
    // one decimal both round to 0.1/0.2 and the gap reads as noise.
    expect(formatPawns(14)).toBe("0.14");
    expect(formatPawns(20)).toBe("0.20");
  });

  it("never leaks binary float noise into the rendered figure", () => {
    // 1.07 and 0.29 are not exactly representable; .toFixed is what
    // stops "1.0700000000000003" reaching a table cell or a tooltip.
    expect(formatPawns(107)).toBe("1.07");
    expect(formatPawns(29)).toBe("0.29");
  });
});

describe("formatPawnLoss", () => {
  it("gives one move's cost to one decimal, matching the coach's prose", () => {
    // coach/prompt.py::format_cp_loss renders this same value as
    // "about 3.1 pawns" in the explanation shown beside it.
    expect(formatPawnLoss(310)).toBe("3.1");
    expect(formatPawnLoss(100)).toBe("1.0");
  });

  it("carries no sign — a loss is a magnitude", () => {
    expect(formatPawnLoss(250)).toBe("2.5");
  });
});
