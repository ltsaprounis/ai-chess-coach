import { describe, expect, it } from "vitest";
import { axisScale } from "./chartTheme";

describe("axisScale", () => {
  it("keeps a zero-based counts axis at integer ticks", () => {
    const scale = axisScale(0, 7, { zeroBased: true, minStep: 1 });
    expect(scale.lo).toBe(0);
    expect(scale.ticks[0]).toBe(0);
    expect(scale.ticks.every((tick) => Number.isInteger(tick))).toBe(true);
    expect(scale.ticks.at(-1)).toBeLessThanOrEqual(scale.hi);
  });

  it("pads a rating axis instead of forcing zero", () => {
    const scale = axisScale(780, 950, { minPad: 10, minStep: 5 });
    expect(scale.lo).toBeLessThan(780);
    expect(scale.lo).toBeGreaterThan(0);
    expect(scale.hi).toBeGreaterThan(950);
    expect(scale.ticks.length).toBeGreaterThanOrEqual(2);
  });

  it("survives a single-valued domain", () => {
    const scale = axisScale(800, 800, { minPad: 10, minStep: 5 });
    expect(scale.lo).toBeLessThan(800);
    expect(scale.hi).toBeGreaterThan(800);
    expect(scale.ticks.length).toBeGreaterThan(0);
  });

  it("handles an all-zero counts axis", () => {
    const scale = axisScale(0, 0, { zeroBased: true, minStep: 1 });
    expect(scale.hi).toBeGreaterThan(0);
    expect(scale.ticks).toContain(0);
  });
});
