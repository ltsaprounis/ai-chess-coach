import { describe, expect, it } from "vitest";
import { clampPage, pageCount, paginationItems } from "./pagination";

describe("pageCount", () => {
  it("keeps an empty list at one page", () => {
    expect(pageCount(0, 20)).toBe(1);
  });

  it("fills pages exactly at the boundary", () => {
    expect(pageCount(20, 20)).toBe(1);
    expect(pageCount(21, 20)).toBe(2);
  });

  it("rounds partial pages up", () => {
    expect(pageCount(45, 20)).toBe(3);
  });
});

describe("clampPage", () => {
  it("keeps an in-range page", () => {
    expect(clampPage(2, 5)).toBe(2);
  });

  it("pulls a remembered page back into a shrunken list", () => {
    expect(clampPage(7, 3)).toBe(3);
  });

  it("floors at page one", () => {
    expect(clampPage(0, 3)).toBe(1);
  });
});

describe("paginationItems", () => {
  it("renders a single page as itself", () => {
    expect(paginationItems(1, 1)).toEqual([1]);
  });

  it("renders short runs in full", () => {
    expect(paginationItems(4, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it("collapses both sides around a middle page", () => {
    expect(paginationItems(5, 20)).toEqual([1, "gap", 4, 5, 6, "gap", 20]);
  });

  it("collapses only the far side at the start", () => {
    expect(paginationItems(1, 20)).toEqual([1, 2, "gap", 20]);
  });

  it("collapses only the far side at the end", () => {
    expect(paginationItems(20, 20)).toEqual([1, "gap", 19, 20]);
  });

  it("omits the gap when the runs touch", () => {
    expect(paginationItems(3, 8)).toEqual([1, 2, 3, 4, "gap", 8]);
  });
});
