import { describe, expect, it } from "vitest";
import type { EvalLine } from "./liveEval";
import { evalSign, formatEval, sideToMove, whiteFraction } from "./liveEval";

const cp = (value: number) => ({ eval_cp: value, eval_mate: null });
const mate = (value: number) => ({ eval_cp: null, eval_mate: value });

/** A full MultiPV line, as one entry in a `LiveEval` snapshot. */
const line = (partial: Partial<EvalLine> = {}): EvalLine => ({
  multipv: 1,
  depth: 18,
  eval_cp: 35,
  eval_mate: null,
  pv_san: ["Nf3", "Nc6"],
  ...partial,
});

describe("formatEval", () => {
  it("formats centipawns as signed pawns", () => {
    expect(formatEval(cp(123))).toBe("+1.23");
    expect(formatEval(cp(-50))).toBe("−0.50");
    expect(formatEval(cp(1000))).toBe("+10.00");
  });

  it("shows equality without a sign", () => {
    expect(formatEval(cp(0))).toBe("0.00");
  });

  it("formats mate as signed moves to mate", () => {
    expect(formatEval(mate(3))).toBe("M 3");
    expect(formatEval(mate(-2))).toBe("−M 2");
  });
});

describe("whiteFraction", () => {
  it("is half at equality", () => {
    expect(whiteFraction(cp(0))).toBe(0.5);
  });

  it("is symmetric around equality", () => {
    expect(whiteFraction(cp(200)) + whiteFraction(cp(-200))).toBeCloseTo(1);
  });

  it("grows with the advantage but stays inside the bar", () => {
    expect(whiteFraction(cp(100))).toBeGreaterThan(0.5);
    expect(whiteFraction(cp(500))).toBeGreaterThan(whiteFraction(cp(100)));
    expect(whiteFraction(cp(10000))).toBeLessThanOrEqual(1);
    expect(whiteFraction(cp(-10000))).toBeGreaterThanOrEqual(0);
  });

  it("pins mate to the mating side's end", () => {
    expect(whiteFraction(mate(5))).toBe(1);
    expect(whiteFraction(mate(-5))).toBe(0);
  });
});

describe("EvalLine snapshot lines", () => {
  it("format and bar helpers work over a full snapshot line", () => {
    const rank2 = line({ multipv: 2, eval_cp: -70, pv_san: ["e4", "e5"] });
    expect(formatEval(rank2)).toBe("−0.70");
    expect(whiteFraction(rank2)).toBeLessThan(0.5);
  });
});

describe("evalSign", () => {
  it("favors white for a positive score", () => {
    expect(evalSign(cp(35))).toBe("white");
  });

  it("favors black for a negative score", () => {
    expect(evalSign(cp(-35))).toBe("black");
  });

  it("is equal only at exactly zero centipawns", () => {
    expect(evalSign(cp(0))).toBe("equal");
  });

  it("follows the mating side for mate scores", () => {
    expect(evalSign(mate(4))).toBe("white");
    expect(evalSign(mate(-4))).toBe("black");
  });
});

describe("sideToMove", () => {
  it("reads white from the starting position", () => {
    expect(
      sideToMove("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ).toBe("white");
  });

  it("reads black after white's first move", () => {
    expect(
      sideToMove("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"),
    ).toBe("black");
  });
});
